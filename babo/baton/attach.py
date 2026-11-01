"""Attach Baton to an AgentLoop whose workspace is a resident directory.

Called from ``AgentLoop.from_config`` (one ``# BATON:`` line). For an
ordinary workspace it does nothing. For a resident it fails closed on anything
that would break the town's invariants, then wires in the resident's tools.
There is deliberately no setting that skips it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from babo.agent.tools.context import current_request_session_key
from babo.baton.checkpoint import CheckpointService
from babo.baton.config import BatonConfig
from babo.baton.events import append_event
from babo.baton.gate import GATED_TOOLS, install_commit_gate
from babo.baton.hooks import MemoryFormation
from babo.baton.registry import Registry, Resident
from babo.baton.tools.babo_adapter import baton_tools
from babo.baton.tools.core import Toolbox, ToolSpec
from babo.baton.town import BatonError, Town, find_town_for_workspace

VISIT_SPEC = ToolSpec(
    "visit",
    "Visit another resident: they take one turn with your message, in their own session, and you get their reply.",
    {
        "type": "object",
        "properties": {
            "resident": {"type": "string", "description": "Resident id or name."},
            "message": {"type": "string", "description": "What you want to say or ask."},
        },
        "required": ["resident", "message"],
    },
    offices=("mayor",),
)
MAX_TOOLS_WARNING = 15  # the Mayor: 15 with request_session

CHECKPOINT_SPEC = ToolSpec(
    "checkpoint",
    "Write a checkpoint of your work now, in your own words. Optional note says why.",
    {"type": "object", "properties": {"note": {"type": "string", "description": "Optional: why now."}}},
)

if TYPE_CHECKING:
    from babo.agent.loop import AgentLoop
    from babo.agent.tools.base import Tool
    from babo.baton.profiles import Profile
    from babo.config.schema import Config


@dataclass
class ResidentRuntime:
    town: Town
    resident: Resident
    config: BatonConfig
    toolbox: Toolbox
    formation: MemoryFormation
    pruned: list[str] = field(default_factory=list)


def resident_for(workspace: Path) -> tuple[Town, str] | None:
    return find_town_for_workspace(workspace)


def validate_config(config: Config) -> None:
    if config.agents.defaults.dream.enabled:
        raise BatonError(
            "Dream is enabled in this config. Residents author their own memory; "
            "set agents.defaults.dream.enabled to false."
        )
    if not config.tools.restrict_to_workspace:
        raise BatonError(
            "tools.restrictToWorkspace must be true for residents: it is what keeps "
            "each resident's directory private."
        )


def attach(loop: AgentLoop, config: Config) -> ResidentRuntime | None:
    found = resident_for(loop.workspace)
    if found is None:
        return None
    town, resident_id = found
    resident = Registry(town).require_active(resident_id)
    validate_config(config)
    baton_cfg: BatonConfig = config.baton
    if baton_cfg.town_path.resolve() != town.root:
        raise BatonError(f"This config's baton.town is {baton_cfg.town_path}, but the workspace is in {town.root}.")

    from babo.baton.context import BatonContextBuilder

    if isinstance(loop.context, BatonContextBuilder):
        loop.context.configure(baton_cfg)

    from babo.baton import profiles

    profile = profiles.load(town, baton_cfg, resident_id, resident.office)
    memory = {k: v for k, v in profile.memory.model_dump().items() if v is not None}
    if memory:
        baton_cfg = baton_cfg.model_copy(update={"memory": baton_cfg.memory.model_copy(update=memory)})
    toolbox = Toolbox(town, resident_id, baton_cfg.index_file)
    formation = MemoryFormation(town, resident_id, baton_cfg, CheckpointService(town, resident_id, baton_cfg))
    runtime = ResidentRuntime(town=town, resident=resident, config=baton_cfg, toolbox=toolbox, formation=formation)

    def checkpoint_tool(note: str | None = None) -> str:
        session_key = current_request_session_key()
        formation.mark_due(session_key, "requested")
        if note:
            append_event(town, resident_id, session_key, "checkpoint_request", note=note)
        return "A checkpoint turn will follow this tool call. Write it in your own words."

    toolbox.extra["checkpoint"] = checkpoint_tool
    if resident.office in VISIT_SPEC.offices:
        toolbox.extra["visit"] = make_visit(config, town, resident_id)

    # Tools: Baton's own, then prune upstream's to what the profile allows.
    for tool in baton_tools(toolbox, {"checkpoint": CHECKPOINT_SPEC, "visit": VISIT_SPEC}):
        loop.tools.register(tool)
    allowed = profiles.harness_tools(profile, baton_cfg, resident_id, resident.office)
    if toolbox.curates_for_archivist():
        allowed |= {n for n in toolbox.available() if n.startswith("commons_")}
    for name in list(loop.tools.tool_names):
        if name not in allowed and not profiles.mcp_permitted(profile, name):
            loop.tools.unregister(name)
            runtime.pruned.append(name)

    # Plus every tool the owner approved for the town.
    from babo.baton.tools.active import active_commons_tools

    for tool in active_commons_tools(town):
        if not loop.tools.has(tool.name):
            loop.tools.register(tool)
            allowed.add(tool.name)
    _guard_registry(loop, allowed, profile)
    if len(loop.tools.tool_names) > MAX_TOOLS_WARNING:
        logger.warning("Baton: {} has {} tools; a small model chooses better from about 12", resident_id, len(loop.tools.tool_names))
    from babo.baton.growth import ensure_project_clones

    ensure_project_clones(town, resident_id)

    # Memory formation: the commit gate, the per-turn hook, compaction, session end.
    for name in GATED_TOOLS:
        shell = loop.tools.get(name)
        if shell is not None:
            install_commit_gate(
                shell, town=town, resident_id=resident_id,
                limit=baton_cfg.memory.commit_gate_after_tool_calls, counters=formation.counters,
            )
    loop._hook_factories.append(formation.hook_factory)  # pyright: ignore[reportPrivateUsage]
    # Where it is now (baton, letters) at the end of the conversation, when it changes; then a clock it can trust, on every turn.
    from babo.baton.clock import clock_provider
    from babo.baton.context import now_provider

    loop.register_runtime_context_provider(now_provider(loop))

    loop.register_runtime_context_provider(clock_provider(town, resident_id))
    from babo.baton.compaction import BatonConsolidator

    if isinstance(loop.consolidator, BatonConsolidator):
        loop.consolidator.formation = formation
    upstream_aclose = loop.aclose

    async def aclose() -> None:
        try:
            await formation.flush()
        finally:
            await upstream_aclose()

    loop.aclose = aclose  # type: ignore[method-assign]
    logger.info(
        "Baton: {} attached as {} with tools {}",
        resident_id, resident.office, sorted(loop.tools.tool_names),
    )
    loop.baton = runtime  # type: ignore[attr-defined]
    return runtime


def _guard_registry(loop: AgentLoop, allowed: set[str], profile: Profile) -> None:
    """Tools registered later (MCP servers connect lazily) must pass the profile too."""
    from babo.baton import profiles

    register = loop.tools.register

    def guarded(tool: Tool) -> None:
        if tool.name in allowed or profiles.mcp_permitted(profile, tool.name):
            register(tool)
        else:
            logger.debug("Baton: {} is not in this resident's profile; not registered", tool.name)

    loop.tools.register = guarded  # type: ignore[method-assign]


def make_visit(config: Config, town: Town, visitor: str):
    """The Mayor's visit: one turn of another resident's own loop, in a session
    reserved for visits from this visitor. The model queue keeps it in line."""

    async def visit(resident: str, message: str) -> str:
        from babo.agent.loop import AgentLoop
        from babo.agent.tools.registry import ToolRegistry

        registry = Registry(town)
        target = registry.resolve(resident)
        if target is None or target.status != "active":
            raise BatonError(f"No resident called '{resident}'.")
        if target.id == visitor:
            raise BatonError("You can't visit yourself.")

        async def in_babo() -> str:
            cfg = config.model_copy(deep=True)
            cfg.agents.defaults.workspace = str(town.resident_dir(target.id))
            other = AgentLoop.from_config(cfg, tool_registry=ToolRegistry())
            try:
                reply = await other.process_direct(
                    f"[visit from {visitor}] {message}", session_key=f"visit:{visitor}",
                    channel="visit", chat_id=visitor, sender_id=visitor,
                )
            finally:
                await other.aclose()
            return str(getattr(reply, "content", "") or "")

        from babo.baton.opencode import resident_turn

        content = (await resident_turn(config, town, target.id, f"[visit from {visitor}] {message}",
                                       kind=f"visit:{visitor}", in_babo=in_babo)).strip() or "(no reply)"
        return f"{target.display} replied:\n{content}"

    return visit
