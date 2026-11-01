"""``baton-mcp``: the Baton tools over MCP stdio.

Started as ``babo mcp --as <resident>``. The resident identity is fixed
when the server starts (from ``--as``, or the ``BATON_RESIDENT`` environment
variable), checked against the registry, and never taken from a tool call.

Residents running inside babo get the same tools in-process; this server is
how residents in opencode get them, and what ``doctor`` checks. Babo's own
tools the resident's profile allows (web search, with Babo's key) are added.
"""

from __future__ import annotations

import asyncio
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from babo.agent.tools.base import Tool
from babo.baton.tools.core import TOOL_SPECS, Toolbox
from babo.baton.town import BatonError


def build_server(toolbox: Toolbox, extra: list[Tool] | None = None) -> Server:
    server: Server = Server(f"baton-{toolbox.resident_id}")
    names = [n for n in toolbox.available() if n in TOOL_SPECS]
    babo_tools = {t.name: t for t in extra or [] if t.name not in names}

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=n, description=TOOL_SPECS[n].description, inputSchema=TOOL_SPECS[n].parameters)
            for n in names
        ] + [types.Tool(name=t.name, description=t.description, inputSchema=t.parameters) for t in babo_tools.values()]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
        if name in babo_tools:
            try:
                result = await babo_tools[name].execute(**(arguments or {}))
            except TypeError as exc:
                result = f"Error: wrong arguments for {name}: {exc}"
            return [types.TextContent(type="text", text=str(result))]
        from babo.baton import aliases

        name = aliases.tool(name)
        if name not in names:
            return [types.TextContent(type="text", text=f"Error: {name} is not available to {toolbox.resident_id}")]
        try:
            text = await asyncio.to_thread(toolbox.call, name, **(arguments or {}))
        except BatonError as exc:
            text = f"Error: {exc}"
        except TypeError as exc:
            text = f"Error: wrong arguments for {name}: {exc}"
        return [types.TextContent(type="text", text=text)]

    return server


async def serve(toolbox: Toolbox, extra: list[Tool] | None = None) -> None:
    server = build_server(toolbox, extra)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
