"""``baton-mcp``: the Baton tools over MCP stdio.

Started as ``nanobot baton mcp --as <resident>``. The resident identity is fixed
when the server starts (from ``--as``, or the ``BATON_RESIDENT`` environment
variable), checked against the registry, and never taken from a tool call.

Residents running inside nanobot get the same tools in-process; this server is
for other harnesses (OpenCode, phase two) and for ``doctor``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from nanobot.baton.tools.core import TOOL_SPECS, Toolbox
from nanobot.baton.town import BatonError


def build_server(toolbox: Toolbox) -> Server:
    server: Server = Server(f"baton-{toolbox.resident_id}")
    names = [n for n in toolbox.available() if n in TOOL_SPECS]

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=n, description=TOOL_SPECS[n].description, inputSchema=TOOL_SPECS[n].parameters)
            for n in names
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
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


async def serve(toolbox: Toolbox) -> None:
    server = build_server(toolbox)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
