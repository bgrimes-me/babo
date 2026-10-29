"""Public tool contracts and discovery interfaces."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from babo.agent.tools.base import Schema, Tool, ToolResult, tool_parameters
    from babo.agent.tools.context import ToolContext
    from babo.agent.tools.loader import ToolLoader
    from babo.agent.tools.registry import ToolRegistry
    from babo.agent.tools.schema import (
        ArraySchema,
        BooleanSchema,
        IntegerSchema,
        NumberSchema,
        ObjectSchema,
        StringSchema,
        tool_parameters_schema,
    )


_EXPORTS = {
    "Schema": "babo.agent.tools.base",
    "ArraySchema": "babo.agent.tools.schema",
    "BooleanSchema": "babo.agent.tools.schema",
    "IntegerSchema": "babo.agent.tools.schema",
    "NumberSchema": "babo.agent.tools.schema",
    "ObjectSchema": "babo.agent.tools.schema",
    "StringSchema": "babo.agent.tools.schema",
    "Tool": "babo.agent.tools.base",
    "ToolContext": "babo.agent.tools.context",
    "ToolLoader": "babo.agent.tools.loader",
    "ToolResult": "babo.agent.tools.base",
    "ToolRegistry": "babo.agent.tools.registry",
    "tool_parameters": "babo.agent.tools.base",
    "tool_parameters_schema": "babo.agent.tools.schema",
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
    "Schema",
    "ArraySchema",
    "BooleanSchema",
    "IntegerSchema",
    "NumberSchema",
    "ObjectSchema",
    "StringSchema",
    "Tool",
    "ToolContext",
    "ToolLoader",
    "ToolResult",
    "ToolRegistry",
    "tool_parameters",
    "tool_parameters_schema",
]
