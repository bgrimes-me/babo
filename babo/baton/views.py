"""The town as plain data, and Squid's decisions as plain calls.

The command line (`babo town`, `babo review`, `babo note`) and the web
interface's town pages both go through these functions, so the two always
show the same thing and decide the same way.
"""

from __future__ import annotations

import json
from typing import Any, Literal, cast

from babo.baton import gitops, growth
from babo.baton.founding import founding_requests
from babo.baton.inbox import unread
from babo.baton.notes import first_line, split_frontmatter
from babo.baton.registry import Registry
from babo.baton.town import BatonError, Town

DecisionKind = Literal["tool", "proposal", "founding", "flag"]


def town_overview(town: Town) -> dict[str, Any]:
    residents: list[dict[str, Any]] = []
    for r in Registry(town).all():
        waiting = unread(town, r.id)
        residents.append({
            "id": r.id, "name": "" if r.name == "unchosen" else r.name, "office": r.office,
            "status": r.status, "founded": r.founded, "last_active": r.last_active,
            "unread": len(waiting), "flags": sum(1 for i in waiting if i.kind == "flag"),
        })
    board: list[dict[str, Any]] = []
    for path in sorted(town.board.glob("*.md")):
        meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
        if meta.get("status", "open") == "open":
            board.append({"kind": str(meta.get("kind", "")), "author": str(meta.get("author", "?")),
                          "created": str(meta.get("created", "")), "text": first_line(body, 200)})
    return {
        "name": town.name, "root": str(town.root), "residents": residents, "board": board,
        "pending": pending_counts(town), "last_night": last_night(town),
    }


def pending_counts(town: Town) -> dict[str, int]:
    return {
        "tools": len(growth.pending_tools(town)), "proposals": len(growth.open_proposals(town)),
        "founding": len(founding_requests(town)), "flags": len(growth.stale_flags(town)),
    }


def last_night(town: Town) -> dict[str, Any] | None:
    ledgers = sorted((town.state / "night").glob("*.json"))
    if not ledgers:
        return None
    try:
        data: object = json.loads(ledgers[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    steps = cast(dict[str, Any], cast(dict[str, Any], data).get("steps") or {})
    night = ledgers[-1].stem
    return {"night": night, "finished": "postpass" in steps, "steps": sorted(steps),
            "log": _read(town.state / "night-logs" / f"{night}.md")[-4000:]}


def review_items(town: Town) -> dict[str, Any]:
    tools = [{
        "id": t.name, "name": t.name, "author": t.author, "description": t.description,
        "tested": t.tested, "isolation": t.isolation, "review": t.review,
        "code": _read(t.folder / "tool.py"), "test": _read(t.folder / "test_tool.py"),
    } for t in growth.pending_tools(town)]
    proposals = [{
        "id": p.path.name, "title": p.title, "author": p.author, "project": p.project,
        "town_name": p.town_name, "review": p.review,
        "body": split_frontmatter(p.path.read_text(encoding="utf-8"))[1].strip(),
    } for p in growth.open_proposals(town)]
    founding: list[dict[str, Any]] = []
    for req in founding_requests(town):
        meta, body = split_frontmatter(req.read_text(encoding="utf-8"))
        founding.append({"id": req.name, "office": str(meta.get("office", "")),
                         "author": str(meta.get("author", "?")), "body": body.strip()})
    flags = [{"id": str(f.get("path", "")), "to": str(f.get("to", "")), "by": str(f.get("by", "")),
              "at": str(f.get("at", "")), "claim": str(f.get("claim", ""))} for f in growth.stale_flags(town)]
    return {"tools": tools, "proposals": proposals, "founding": founding, "flags": flags}


def decide(town: Town, kind: DecisionKind, item_id: str, *, yes: bool, reason: str, commit: bool = True) -> str:
    """Apply one of Squid's review decisions. Returns a short plain-language result."""
    reason = reason.strip() or "(no reason given)"
    if kind == "tool":
        growth.decide_tool(town, item_id, approve=yes, reason=reason)
        result = f"Tool {item_id} {'approved' if yes else 'rejected'}."
    elif kind == "proposal":
        growth.decide_proposal(town, _child(town.proposals / "open", item_id), accept=yes, reason=reason)
        result = f"Proposal {'accepted' if yes else 'declined'}." + (f" The town is now called {town.name}." if yes and town.name else "")
    elif kind == "founding":
        new_id = growth.decide_founding(town, _child(town.board, item_id), approve=yes, reason=reason)
        result = f"Founded: {new_id}." if new_id else "Founding request declined."
    elif kind == "flag":
        if not yes:
            return "Flag kept open."
        growth.resolve_flag(town, item_id, note=reason)
        result = "Flag marked resolved."
    else:  # pragma: no cover - the type admits nothing else
        raise BatonError(f"Unknown decision kind: {kind}")
    if commit:
        gitops.commit_all(town, f"Review by Squid: {result}")
    return result


def name_town(town: Town, name: str) -> str:
    named = town.set_name(name, by="squid")
    gitops.commit_all(town, f"Name the town: {named}")
    return f"The town is now called {named}."


def leave_note(town: Town, resident: str, text: str) -> str:
    if not text.strip():
        raise BatonError("The note is empty.")
    growth.leave_note(town, resident, text.strip())
    return f"Note left for {resident}."


def _child(folder: Any, name: str) -> Any:
    """A file directly inside ``folder``; anything that would leave it is refused."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise BatonError(f"'{name}' is not an item in the review.")
    path = folder / name
    if not path.is_file():
        raise BatonError(f"'{name}' is no longer waiting for review.")
    return path


def _read(path: Any) -> str:
    try:
        return str(path.read_text(encoding="utf-8"))
    except OSError:
        return ""
