"""Noticing when an agent goes round in circles.

Small models sometimes loop: the same call with the same arguments again and
again, or a tool tried again right after it was refused. The harness watches
for both (it sees every call) and answers with one short reminder, placed with
the result of the call that tripped it, where the model looks. If the same
thing happens again in that session, the owner is told: the agent is stuck,
and a person can help.

The same rules run in Babo (for the Mayor) and in Babo's plugin for opencode.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any, cast

EVENT = "drift"
REPEATS = 3  # the same call this many times ...
WINDOW = 6  # ... among the last this many calls
REFUSED = ("permission denied", "permission.rejected", "blocked", "not allowed", "is not available")


def signature(tool: str, args: dict[str, Any]) -> str:
    return f"{tool} {json.dumps(args, sort_keys=True, ensure_ascii=False)}"


def refused(text: str) -> bool:
    low = text.lower()
    return any(word in low for word in REFUSED)


def reminder(rule: str, tool: str) -> str:
    if rule == "repeat":
        what = f"You've made the same `{tool}` call {REPEATS} times; it will give the same answer."
    else:
        what = f"`{tool}` was refused, and trying it again will be refused again."
    return (f"[Babo: {what} Step back: what do you actually need? Try a different way, or say plainly what's in the way "
            "and stop there.]")


@dataclass
class Watch:
    """The recent calls of each session, and which rules have tripped there."""

    recent: dict[str, deque[tuple[str, bool]]] = field(default_factory=lambda: cast(dict[str, deque[tuple[str, bool]]], {}))
    tripped: dict[str, dict[str, int]] = field(default_factory=lambda: cast(dict[str, dict[str, int]], {}))

    def see(self, session: str, tool: str, args: dict[str, Any], *, failed: bool, result: str) -> tuple[str, int] | None:
        """Record a call. Returns (rule, how many times it has tripped in this session) when one trips."""
        calls = self.recent.setdefault(session, deque(maxlen=WINDOW))
        sig = signature(tool, args)
        was_refused = failed and refused(result)
        refused_before = any(s.split(" ", 1)[0] == tool and r for s, r in calls)
        calls.append((sig, was_refused))
        rule = ""
        if sum(1 for s, _ in calls if s == sig) >= REPEATS:
            rule = "repeat"
            calls.clear()  # one reminder per run of repeats
        elif refused_before and was_refused:
            rule = "refused_again"
            calls.clear()
        if not rule:
            return None
        counts = self.tripped.setdefault(session, {})
        counts[rule] = counts.get(rule, 0) + 1
        return rule, counts[rule]


def tell_owner(town: Any, resident: str, session: str, title: str, rule: str, tool: str) -> None:
    """The second time in a session: the owner hears that the agent seems stuck."""
    from babo.baton import notices

    doing = f"repeating the same `{tool}` call" if rule == "repeat" else f"trying `{tool}` again after it was refused"
    where = f" in “{title}”" if title else ""
    notices.add(town, "stuck", f"{resident} seems stuck{where}: it keeps {doing}. It may need a hand, or more access.",
                resident=resident, session=session)
