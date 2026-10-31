"""Notices for the owner: what happened in the town that they'd want to know about.

A resident finished a session and wrote its memory; a resident is waiting for an
answer. The web interface shows new ones in every chat until they're read, and
``babo notices`` lists them in the terminal. They are kept with the town's
runtime state (not its history).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from filelock import FileLock

from babo.baton.town import Town, now_iso

KEEP = 200


def _path(town: Town) -> Path:
    return town.state / "runtime" / "notices.jsonl"


def _load(town: Town) -> list[dict[str, Any]]:
    try:
        lines = _path(town).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(cast(dict[str, Any], item))
    return out


def _save(town: Town, items: list[dict[str, Any]]) -> None:
    path = _path(town)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items[-KEEP:]), encoding="utf-8")
    tmp.replace(path)


def add(town: Town, kind: str, text: str, *, resident: str = "", session: str = "") -> dict[str, Any]:
    with FileLock(str(town.locks / "notices.lock"), timeout=10):
        items = _load(town)
        item = {"id": f"{now_iso()}-{len(items)}", "at": now_iso(), "kind": kind, "text": text,
                "resident": resident, "session": session, "read": False}
        _save(town, [*items, item])
    return item


def recent(town: Town, *, unread_only: bool = False, limit: int = 20) -> list[dict[str, Any]]:
    items = [i for i in _load(town) if not (unread_only and i.get("read"))]
    return list(reversed(items))[:limit]


def mark_read(town: Town, ids: list[str] | None = None) -> int:
    """Mark these notices (or all of them) as read. Returns how many changed."""
    with FileLock(str(town.locks / "notices.lock"), timeout=10):
        items = _load(town)
        changed = 0
        for item in items:
            if not item.get("read") and (ids is None or item.get("id") in ids):
                item["read"] = True
                changed += 1
        if changed:
            _save(town, items)
    return changed
