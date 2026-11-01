"""Allow all: the owner lets one of the Mayor's chats send residents on errands without review.

Normally every session the Mayor asks for waits for the owner's approval. With
"allow all" on for one chat (a web chat, a Discord channel, the terminal),
requests the Mayor makes from that chat are approved as they are asked, and
the upkeep service runs them within a minute, with whatever access he asked
for. They still run in a resident's opencode session, and every request and
result is still recorded and listed in the review's "recently run".

It is the owner's choice, per chat, for an hour, a few hours, or until they turn it
off; turning it on and off is recorded in the town's history.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from babo.baton import gitops
from babo.baton.town import OWNER, BatonError, Town, atomic_write, now_iso

MAX_KEY = 200


def _path(town: Town) -> Path:
    return town.state / "allow-all.json"


def _load(town: Town) -> dict[str, dict[str, str]]:
    try:
        loaded: object = json.loads(_path(town).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cast(dict[str, dict[str, str]], loaded) if isinstance(loaded, dict) else {}


def _when(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def active(town: Town, *, now: datetime | None = None) -> dict[str, dict[str, str]]:
    """The chats where allow-all is on now (expired ones are left out)."""
    now = now or datetime.now().astimezone()
    out: dict[str, dict[str, str]] = {}
    for key, grant in _load(town).items():
        until = _when(grant.get("until", "")) if grant.get("until") else None
        if until is None or until > now:
            out[key] = grant
    return out


def is_on(town: Town, session_key: str | None, *, now: datetime | None = None) -> bool:
    return bool(session_key) and session_key in active(town, now=now)


def turn_on(town: Town, session_key: str, *, hours: float | None) -> str:
    key = session_key.strip()
    if not key or len(key) > MAX_KEY or ":" not in key:
        raise BatonError("Give the chat's session key, like websocket:<id> or discord:<channel>.")
    if hours is not None and not 0 < hours <= 24 * 7:
        raise BatonError("Allow all can last from a few minutes to a week, or until you turn it off.")
    now = datetime.now().astimezone()
    until = (now + timedelta(hours=hours)).isoformat(timespec="seconds") if hours is not None else ""
    grants = {k: v for k, v in active(town, now=now).items()}
    grants[key] = {"since": now_iso(), "until": until, "by": OWNER}
    atomic_write(_path(town), json.dumps(grants, indent=2) + "\n")
    lasting = f"until {until[:16].replace('T', ' ')}" if until else "until you turn it off"
    gitops.commit_paths(town, [_path(town)], f"{town.owner_name} turned allow-all on for {key} ({lasting})")
    return f"Allow all is on for {key}, {lasting}. The Mayor's errands from this chat run without your review."


def turn_off(town: Town, session_key: str) -> str:
    key = session_key.strip()
    grants = active(town)
    if key not in grants:
        return f"Allow all wasn't on for {key}."
    del grants[key]
    atomic_write(_path(town), json.dumps(grants, indent=2) + "\n")
    gitops.commit_paths(town, [_path(town)], f"{town.owner_name} turned allow-all off for {key}")
    return f"Allow all is off for {key}. The Mayor's errands wait for your review again."


def status(town: Town) -> dict[str, Any]:
    return {"active": active(town)}
