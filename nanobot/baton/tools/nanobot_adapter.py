"""Expose Baton tools to a nanobot AgentLoop (in-process)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.baton.tools.core import TOOL_SPECS, Toolbox, ToolSpec
from nanobot.baton.town import BatonError


class BatonTool(Tool):
    _plugin_discoverable = False

    def __init__(self, spec: ToolSpec, run: Callable[..., str]):
        self._spec = spec
        self._run = run

    @property
    def name(self) -> str:  # pyright: ignore[reportIncompatibleMethodOverride]
        return self._spec.name

    @property
    def description(self) -> str:  # pyright: ignore[reportIncompatibleMethodOverride]
        return self._spec.description

    @property
    def parameters(self) -> dict[str, Any]:  # pyright: ignore[reportIncompatibleMethodOverride]
        return self._spec.parameters

    @property
    def read_only(self) -> bool:
        return self._spec.read_only

    async def execute(self, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(self._run, **kwargs)
        except BatonError as exc:
            return Tool.error(f"Error: {exc}")
        except TypeError as exc:
            return Tool.error(f"Error: wrong arguments for {self.name}: {exc}")
        except Exception as exc:  # a broken tool must not end the turn
            logger.exception("Baton tool {} failed", self.name)
            return Tool.error(f"Error: {self.name} failed: {type(exc).__name__}: {exc}")


def baton_tools(toolbox: Toolbox, extra_specs: dict[str, ToolSpec] | None = None) -> list[BatonTool]:
    """One BatonTool per tool this resident's office may use."""
    specs = {**TOOL_SPECS, **(extra_specs or {})}
    out: list[BatonTool] = []
    for name in toolbox.available():
        spec = specs.get(name)
        if spec is None:
            continue

        def run(_name: str = name, **kwargs: Any) -> str:
            return toolbox.call(_name, **kwargs)

        out.append(BatonTool(spec, run))
    return out


__all__ = ["BatonTool", "baton_tools", "BatonError"]
