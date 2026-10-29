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

from nanobot.agent.tools.context import current_request_session_key
from nanobot.baton.checkpoint import CheckpointService
from nanobot.baton.config import BatonConfig
from nanobot.baton.events import append_event
from nanobot.baton.gate import GATED_TOOLS, install_commit_gate
from nanobot.baton.hooks import MemoryFormation
from nanobot.baton.registry import Registry, Resident
from nanobot.baton.tools.core import Toolbox, ToolSpec
from nanobot.baton.tools.nanobot_adapter import baton_tools
from nanobot.baton.town import BatonError, Town, find_town_for_workspace

CHECKPOINT_SPEC = ToolSpec(
    "checkpoint",
    "Write a checkpoint of your work now, in your own words. Optional note says why.",
    {"type": "object", "properties": {"note": {"type": "string", "description": "Optional: why now."}}},
)

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop
    from nanobot.config.schema import Config


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

    from nanobot.baton.context import BatonContextBuilder

    if isinstance(loop.context, BatonContextBuilder):
        loop.context.configure(baton_cfg)

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

    # Tools: Baton's own, then prune upstream's to the office's toolset.
    for tool in baton_tools(toolbox, {"checkpoint": CHECKPOINT_SPEC}):
        loop.tools.register(tool)
    allowed = set(baton_cfg.toolset_for(resident_id, resident.office))
    for name in list(loop.tools.tool_names):
        if name not in allowed:
            loop.tools.unregister(name)
            runtime.pruned.append(name)

    # Memory formation: the commit gate, the per-turn hook, compaction, session end.
    for name in GATED_TOOLS:
        shell = loop.tools.get(name)
        if shell is not None:
            install_commit_gate(
                shell, town=town, resident_id=resident_id,
                limit=baton_cfg.memory.commit_gate_after_tool_calls, counters=formation.counters,
            )
    loop._hook_factories.append(formation.hook_factory)  # pyright: ignore[reportPrivateUsage]
    from nanobot.baton.compaction import BatonConsolidator

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
