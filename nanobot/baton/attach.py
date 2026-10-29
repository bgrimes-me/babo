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

from nanobot.baton.config import BatonConfig
from nanobot.baton.registry import Registry, Resident
from nanobot.baton.tools.core import Toolbox
from nanobot.baton.tools.nanobot_adapter import baton_tools
from nanobot.baton.town import BatonError, Town, find_town_for_workspace

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop
    from nanobot.config.schema import Config


@dataclass
class ResidentRuntime:
    town: Town
    resident: Resident
    config: BatonConfig
    toolbox: Toolbox
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
    runtime = ResidentRuntime(town=town, resident=resident, config=baton_cfg, toolbox=toolbox)

    # Tools: Baton's own, then prune upstream's to the office's toolset.
    for tool in baton_tools(toolbox):
        loop.tools.register(tool)
    allowed = set(baton_cfg.toolset_for(resident_id, resident.office))
    for name in list(loop.tools.tool_names):
        if name not in allowed:
            loop.tools.unregister(name)
            runtime.pruned.append(name)
    logger.info(
        "Baton: {} attached as {} with tools {}",
        resident_id, resident.office, sorted(loop.tools.tool_names),
    )
    loop.baton = runtime  # type: ignore[attr-defined]
    return runtime
