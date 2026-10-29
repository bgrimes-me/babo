"""The commit gate: no `git commit` on a stale baton.

AgentHooks can't veto a tool call, so the shell tools' ``execute`` is wrapped
in place (the tool object stays the same, so upstream code that checks its
type keeps working). When a command would run `git commit` and the resident's
last checkpoint is more than ``commit_gate_after_tool_calls`` tool calls old,
the command is not run and the resident is told, plainly, that a checkpoint
clears it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from babo.agent.tools.base import Tool
from babo.baton.events import append_event, command_of, is_commit_command
from babo.baton.runtime_state import CounterStore
from babo.baton.town import Town

GATED_TOOLS = ("exec", "exec_session")
_MARK = "_baton_commit_gate"


def blocked_message(tool_name: str, args: dict[str, Any], *, town: Town, resident_id: str, limit: int, counters: CounterStore) -> str | None:
    command = command_of(tool_name, args)
    if not command or not is_commit_command(command):
        return None
    age = counters.load().calls_since_checkpoint
    if age <= limit:
        return None
    from babo.agent.tools.context import current_request_session_key

    append_event(town, resident_id, current_request_session_key(), "gate", command=command, calls_since_checkpoint=age)
    return (
        f"Commit paused by the Baton harness: your last checkpoint was {age} tool calls ago (the limit is {limit}). "
        "Call checkpoint() to write one; that clears this, and then run the commit again."
    )


def install_commit_gate(tool: Tool, *, town: Town, resident_id: str, limit: int, counters: CounterStore) -> None:
    if getattr(tool, _MARK, False):
        return
    inner: Callable[..., Awaitable[Any]] = tool.execute
    name = tool.name

    async def execute(**kwargs: Any) -> Any:
        message = blocked_message(name, kwargs, town=town, resident_id=resident_id, limit=limit, counters=counters)
        if message is not None:
            return Tool.error(message)
        return await inner(**kwargs)

    object.__setattr__(tool, "execute", execute)
    object.__setattr__(tool, _MARK, True)
