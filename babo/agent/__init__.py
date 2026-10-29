"""Agent core module."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from babo.agent.context import ContextBuilder
    from babo.agent.hook import (
        AgentHook,
        AgentHookContext,
        AgentRunHookContext,
        AgentTurnHookContext,
        AgentTurnHookFactory,
        CompositeHook,
    )
    from babo.agent.loop import AgentLoop
    from babo.agent.memory import MemoryStore
    from babo.agent.skills import SkillsLoader
    from babo.agent.subagent import SubagentManager


_EXPORTS = {
    "AgentHook": "babo.agent.hook",
    "AgentHookContext": "babo.agent.hook",
    "AgentRunHookContext": "babo.agent.hook",
    "AgentTurnHookContext": "babo.agent.hook",
    "AgentTurnHookFactory": "babo.agent.hook",
    "AgentLoop": "babo.agent.loop",
    "CompositeHook": "babo.agent.hook",
    "ContextBuilder": "babo.agent.context",
    "MemoryStore": "babo.agent.memory",
    "SkillsLoader": "babo.agent.skills",
    "SubagentManager": "babo.agent.subagent",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = [
    "AgentHook",
    "AgentHookContext",
    "AgentRunHookContext",
    "AgentTurnHookContext",
    "AgentTurnHookFactory",
    "AgentLoop",
    "CompositeHook",
    "ContextBuilder",
    "MemoryStore",
    "SkillsLoader",
    "SubagentManager",
]
