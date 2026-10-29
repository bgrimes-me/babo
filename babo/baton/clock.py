"""A clock residents can trust.

A small model should never work out the time from message envelopes: delivery
lag looks exactly like "days have passed" from the inside. So every turn a
resident takes carries the machine's clock and how long ago the resident was
last active, as upstream runtime context (appended to the current message, so
the system prompt stays byte-stable for llama.cpp's prompt cache).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from babo.baton.registry import Registry
from babo.baton.town import Town
from babo.runtime_context import RuntimeContextBlock, wrap_runtime_context_lines

if TYPE_CHECKING:
    from babo.agent.tools.context import RequestContext

CLOCK_SOURCE = "baton_clock"


def stamp(moment: datetime) -> str:
    """``Thursday 2026-10-29 04:31 (UTC+01:00)``"""
    offset = moment.strftime("%z")
    zone = f"UTC{offset[:3]}:{offset[3:]}" if offset else "local time"
    return f"{moment:%A %Y-%m-%d %H:%M} ({zone})"


def ago(then: datetime, now: datetime) -> str:
    seconds = int((now - then).total_seconds())
    if seconds < 0:
        return "in the future (the clock may have changed)"
    if seconds < 90:
        return "just now"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} minutes ago"
    hours = minutes // 60
    if hours < 36:
        return f"{hours} hours ago"
    days = hours // 24
    return f"{days} days ago"


def clock_lines(town: Town, resident_id: str, now: datetime | None = None) -> list[str]:
    now = now or datetime.now().astimezone()
    lines = [f"Now: {stamp(now)}, from the machine's clock. Trust this over any sense of elapsed time."]
    resident = Registry(town).get(resident_id)
    last = resident.last_active if resident else ""
    if last:
        try:
            then = datetime.fromisoformat(last)
        except ValueError:
            then = None
        if then is not None:
            if then.tzinfo is None:
                then = then.astimezone()
            lines.append(f"You were last active: {stamp(then)}, {ago(then, now)}.")
    else:
        lines.append("You have not been active before.")
    return lines


def clock_provider(town: Town, resident_id: str):
    async def provide(request: RequestContext) -> RuntimeContextBlock:
        del request
        return RuntimeContextBlock(source=CLOCK_SOURCE, content=wrap_runtime_context_lines(clock_lines(town, resident_id)))

    return provide
