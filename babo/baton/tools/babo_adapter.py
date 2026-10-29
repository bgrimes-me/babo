"""Expose Baton tools to a babo AgentLoop (in-process)."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from loguru import logger

from babo.agent.tools.base import Tool
from babo.baton.tools.core import TOOL_SPECS, Toolbox, ToolSpec
from babo.baton.town import BatonError


class BatonTool(Tool):
    _plugin_discoverable = False

    def __init__(self, spec: ToolSpec, run: Callable[..., Any], *, is_async: bool = False):
        self._spec = spec
        self._run = run
        self._async = is_async

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
            if self._async:
                return await self._run(**kwargs)
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

        direct = toolbox.extra.get(name)
        if direct is not None and inspect.iscoroutinefunction(direct):
            out.append(BatonTool(spec, direct, is_async=True))
            continue

        def run(_name: str = name, **kwargs: Any) -> str:
            return toolbox.call(_name, **kwargs)

        out.append(BatonTool(spec, run))
    return out


__all__ = ["BatonTool", "baton_tools", "BatonError"]
