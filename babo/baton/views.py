"""The town as plain data, and the owner's decisions as plain calls.

The command line (`babo town`, `babo review`, `babo note`, `babo profile`) and the web
interface's town pages both go through these functions, so the two always
show the same thing and decide the same way.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal, cast

from babo.baton import gitops, growth
from babo.baton.config import BatonConfig
from babo.baton.founding import founding_requests
from babo.baton.inbox import unread
from babo.baton.notes import first_line, split_frontmatter
from babo.baton.registry import Registry
from babo.baton.town import OWNER, BatonError, Town

if TYPE_CHECKING:
    from babo.config.schema import Config

DecisionKind = Literal["tool", "proposal", "founding", "flag", "session"]


def town_overview(town: Town, cfg: BatonConfig) -> dict[str, Any]:
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
        "pending": pending_counts(town), "last_upkeep": last_upkeep(town), "upkeep": upkeep_status(town, cfg),
    }


def pending_counts(town: Town) -> dict[str, int]:
    return {
        "tools": len(growth.pending_tools(town)), "proposals": len(growth.open_proposals(town)),
        "founding": len(founding_requests(town)), "flags": len(growth.stale_flags(town)),
        "sessions": len(_requests().pending(town)),
    }


def last_upkeep(town: Town) -> dict[str, Any] | None:
    """The latest upkeep pass (or, in a town from before upkeep, the latest night)."""
    ledgers = sorted(p for p in (town.state / "upkeep").glob("*.json") if p.name != "scheduler.json")
    logs = town.state / "upkeep-logs"
    if not ledgers:
        ledgers, logs = sorted((town.state / "night").glob("*.json")), town.state / "night-logs"
    if not ledgers:
        return None
    try:
        data: object = json.loads(ledgers[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    steps = cast(dict[str, Any], cast(dict[str, Any], data).get("steps") or {})
    name = ledgers[-1].stem
    from babo.baton.night import pass_label

    return {"pass": pass_label(name),
            "finished": "postpass" in steps, "steps": sorted(steps),
            "log": _read(logs / f"{name[:10]}.md")[-4000:]}


def upkeep_status(town: Town, cfg: BatonConfig) -> dict[str, Any]:
    from babo.baton import upkeep

    return upkeep.status(town, cfg)


def request_upkeep(town: Town) -> str:
    from babo.baton import upkeep

    return upkeep.request_catch_up(town)


def _requests() -> Any:
    from babo.baton import requests

    return requests


def review_items(town: Town, config: Config | None = None) -> dict[str, Any]:
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
    sessions = [_requests().describe(town, config, r) for r in _requests().pending(town)] if config is not None else []
    recent = [{"id": r.id, "resident": r.resident, "task": first_line(r.task, 140), "status": r.status,
               "decided": r.decided, "decided_by": r.decided_by, "commands": r.commands, "access": r.access,
               "result": first_line(r.result, 300)} for r in _requests().recent(town)]
    return {"sessions": sessions, "recent": recent, "tools": tools, "proposals": proposals, "founding": founding, "flags": flags}


def mayor_chats(town: Town, *, limit: int = 10) -> list[dict[str, str]]:
    """The Mayor's recent chats (web, chat apps, terminal), newest first, from his event log."""
    from babo.baton.events import resident_events

    latest: dict[str, str] = {}
    for event in resident_events(town, "mayor"):
        key = str(event.get("session", ""))
        if not key or key.startswith(("night:", "visit:", "opencode:", "doctor:")) or key == "direct":
            continue
        at = str(event.get("at", ""))
        if at > latest.get(key, ""):
            latest[key] = at
    return [{"key": k, "last_active": v} for k, v in sorted(latest.items(), key=lambda kv: kv[1], reverse=True)[:limit]]


def decide(town: Town, kind: DecisionKind, item_id: str, *, yes: bool, reason: str, commit: bool = True,
           digest: str | None = None) -> str:
    """Apply one of the owner's review decisions. Returns a short plain-language result.
    ``digest``: for a session request, the fingerprint of the request as the owner saw it."""
    reason = reason.strip() or "(no reason given)"
    if kind == "session":
        return _requests().decide(town, item_id, approve=yes, reason=reason, digest=digest, commit=commit)
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
        gitops.commit_all(town, f"Review by {town.owner_name}: {result}")
    return result


def name_town(town: Town, name: str) -> str:
    named = town.set_name(name, by=OWNER)
    gitops.commit_all(town, f"Name the town: {named}")
    return f"The town is now called {named}."


def leave_note(town: Town, resident: str, text: str) -> str:
    if not text.strip():
        raise BatonError("The note is empty.")
    growth.leave_note(town, resident, text.strip())
    return f"Note left for {resident}."


# -- profiles ------------------------------------------------------------------

def _office(town: Town, agent: str) -> str:
    from babo.baton import profiles

    if agent == profiles.NEW_RESIDENTS:
        return "default"
    return Registry(town).require_active(agent).office


def profile_agent(name: str) -> str:
    """``new-residents`` (what people type) names the template new residents start from."""
    from babo.baton import profiles

    return profiles.NEW_RESIDENTS if name.strip() in ("new-residents", profiles.NEW_RESIDENTS) else name.strip()


def profiles_overview(town: Town, cfg: BatonConfig, mcp_known: list[str]) -> list[dict[str, Any]]:
    """The Mayor first (the main agent), then the residents, then the template for new ones."""
    from babo.baton import profiles

    agents = sorted((r.id for r in Registry(town).all() if r.status == "active"), key=lambda a: (a != profiles.MAYOR, a))
    out = [profiles.describe(town, cfg, a, _office(town, a), mcp_known=mcp_known) for a in agents]
    out.append(profiles.describe(town, cfg, profiles.NEW_RESIDENTS, "default", mcp_known=mcp_known))
    return out


def update_profile(town: Town, cfg: BatonConfig, agent: str, changes: dict[str, object], mcp_known: list[str],
                   presets_known: list[str] | None = None) -> str:
    from babo.baton import profiles

    agent = profile_agent(agent)
    said = profiles.update(town, cfg, agent, _office(town, agent), changes, mcp_known=mcp_known, presets_known=presets_known)
    if not said:
        return "Nothing changed."
    if agent == profiles.NEW_RESIDENTS:
        return "Profile for new residents: " + "; ".join(said) + ". Residents founded from now on start with it."
    if agent == profiles.MAYOR:
        return ("Profile of the Mayor: " + "; ".join(said)
                + ". It applies once Babo restarts (on the host: systemctl --user restart babo-gateway).")
    return f"Profile of {agent}: " + "; ".join(said) + ". It applies from the agent's next session."


# -- residents' sessions in opencode -----------------------------------------------

def sessions_overview(town: Town, config: Config, *, archived: bool = False) -> dict[str, Any]:
    from babo.baton import opencode

    residents = [r for r in Registry(town).all() if r.status == "active" and opencode.runs_in_opencode(r.office)]
    from babo.baton.gateway import presets_overview

    return {
        "presets": presets_overview(config),
        "opencode": opencode.opencode_bin(config) is not None,
        "residents": [{"id": r.id, "name": "" if r.name == "unchosen" else r.name, "office": r.office,
                       "sessions": opencode.overview(town, r.id, archived=archived)} for r in residents],
    }


def start_session(town: Town, config: Config, resident: str, *, title: str, overrides: dict[str, object], config_path: Any,
                  task: str = "") -> str:
    """Start a session. With a task, the resident starts on it right away and works on its own."""
    from babo.baton import opencode
    from babo.baton.notes import first_line

    task = task.strip()
    session = opencode.new_session(town, config, resident, title=title.strip() or first_line(task, 80), overrides=overrides)
    if task:
        session.task = task
        opencode.save_session(town, session)
    opencode.start(town, config, session, config_path=config_path)
    if task:
        opencode.prompt(town, config, opencode.get_session(town, resident, session.id), task, config_path=config_path)
        return f"{resident} is working on “{session.title}”. Open the session to watch or join in."
    return f"{resident}'s session “{session.title}” is running."


def send_to_session(town: Town, config: Config, resident: str, session_id: str, text: str, *, config_path: Any) -> str:
    from babo.baton import opencode

    opencode.prompt(town, config, opencode.get_session(town, resident, session_id), text, config_path=config_path)
    return f"Sent to {resident}."


def stop_session_turn(town: Town, resident: str, session_id: str) -> str:
    from babo.baton import opencode

    stopped = opencode.interrupt(town, opencode.get_session(town, resident, session_id))
    return f"Stopped what {resident} was doing; the session is still open." if stopped else f"{resident} wasn't in the middle of anything."


def checkpoint_session(town: Town, config: Config, resident: str, session_id: str, *, config_path: Any) -> str:
    """Ask for a checkpoint now. It takes the model a while, so it runs in the background."""
    import threading

    from babo.baton import opencode

    session = opencode.get_session(town, resident, session_id)
    if opencode.running(town, session) is None:
        raise BatonError("That session isn't running; resume it first, or freeze it to have it reviewed.")

    def write() -> None:
        try:
            opencode.checkpoint(town, config, session, trigger="requested", config_path=config_path)
        except Exception as exc:  # the safety net reviews it later; say why in the log
            from loguru import logger

            logger.warning("Babo: {}'s checkpoint for {} didn't go through: {}", resident, session_id, exc)

    threading.Thread(target=write, name=f"checkpoint-{resident}-{session_id}", daemon=True).start()
    return f"{resident} is writing a checkpoint of this session in its own words."


def set_session_model(town: Town, config: Config, resident: str, session_id: str, preset: str, *, config_path: Any) -> str:
    from babo.baton import opencode

    session = opencode.switch_model(town, config, opencode.get_session(town, resident, session_id), preset, config_path=config_path)
    chosen = str(session.overrides.get("model") or "the profile's model")
    return f"{resident}'s session now uses {chosen}" + (", and carries on where it was." if opencode.running(town, session) else ".")


def change_session_access(town: Town, config: Config, resident: str, session_id: str, changes: dict[str, str], *,
                          config_path: Any, everything: bool = False) -> str:
    """Change what a session may do, now: the same in the web interface and `babo session set`."""
    from babo.baton import opencode, profiles

    session = opencode.get_session(town, resident, session_id)
    before = opencode.effective_profile(town, config, resident, session.overrides, by=session.by)
    wanted = {**(opencode.allow_everything() if everything else {}), **changes}
    if not wanted:
        raise BatonError("Nothing to change: give settings such as shell=allow, or allow everything.")
    session = opencode.change_session(town, config, session, wanted, config_path=config_path)
    after = opencode.effective_profile(town, config, resident, session.overrides, by=session.by)
    said = profiles.describe_changes(before, after)
    if not said:
        return f"“{session.title}” already had those settings."
    carried = ", and carries on where it was" if opencode.running(town, session) else ""
    return f"“{session.title}” now: " + "; ".join(said) + carried + "."


def answer_session_question(town: Town, resident: str, session_id: str, request_id: str, decision: str) -> str:
    from babo.baton import opencode

    opencode.answer_permission(town, opencode.get_session(town, resident, session_id), request_id, decision)
    return {"once": "Allowed, this once.", "always": "Allowed for the rest of this session.", "reject": "Refused."}[decision]


def resume_session(town: Town, config: Config, resident: str, session_id: str, *, config_path: Any) -> str:
    from babo.baton import opencode

    session = opencode.get_session(town, resident, session_id)
    opencode.start(town, config, session, config_path=config_path)
    return f"{resident}'s session “{session.title}” is running again, where it left off."


def freeze_session(town: Town, config: Config, resident: str, session_id: str, *, config_path: Any) -> str:
    from babo.baton import opencode

    session = opencode.get_session(town, resident, session_id)
    was = opencode.freeze(town, session, review=config, config_path=config_path)
    reviewed = opencode.get_session(town, resident, session_id).reviewed
    if not was:
        return f"“{session.title}” wasn't running."
    return f"{resident} reviewed “{session.title}” into its memory, and the session is frozen." if reviewed != session.reviewed \
        else f"{resident}'s session “{session.title}” is frozen; its review didn't go through, so upkeep will do it."


def session_link(town: Town, resident: str, session_id: str) -> str:
    from babo.baton import opencode

    return opencode.pairing_link(town, opencode.get_session(town, resident, session_id))


def archive_session(town: Town, resident: str, session_id: str, *, archived: bool = True) -> str:
    """Move a session to the archive (or back). Only a frozen one: archiving tidies the list, it doesn't stop anything."""
    from babo.baton import opencode

    session = opencode.get_session(town, resident, session_id)
    if archived and opencode.running(town, session) is not None:
        raise BatonError("Freeze the session first; archiving only tidies away sessions that are done.")
    session.archived = archived
    opencode.save_session(town, session)
    return f"“{session.title}” is archived." if archived else f"“{session.title}” is back in the list."


def clean_tags(tags: list[str]) -> list[str]:
    """Tags as the owner typed them, tidied: trimmed, without '#', no repeats, a sensible number."""
    out: list[str] = []
    for tag in tags:
        tag = " ".join(str(tag).strip().lstrip("#").split())[:40]
        if tag and tag.lower() not in {t.lower() for t in out}:
            out.append(tag)
    return out[:12]


def organize_session(town: Town, resident: str, session_id: str, *, title: str | None = None,
                     pinned: bool | None = None, tags: list[str] | None = None) -> str:
    """Rename, pin or tag a session: the same for a resident's session as for a chat with the Mayor."""
    from babo.baton import opencode

    session = opencode.get_session(town, resident, session_id)
    said: list[str] = []
    if title is not None:
        if not title.strip():
            raise BatonError("A session needs a name.")
        session.title = " ".join(title.split())[:120]
        said.append(f"renamed “{session.title}”")
    if pinned is not None:
        session.pinned = pinned
        said.append("pinned" if pinned else "unpinned")
    if tags is not None:
        session.tags = clean_tags(tags)
        said.append(f"tagged {', '.join(session.tags)}" if session.tags else "untagged")
    if not said:
        raise BatonError("Nothing to change: give a new name, pin or unpin it, or set its tags.")
    opencode.save_session(town, session)
    return f"{resident}'s session is {' and '.join(said)}."


def remove_session(town: Town, resident: str, session_id: str) -> str:
    from babo.baton import opencode

    session = opencode.get_session(town, resident, session_id)
    opencode.remove_session(town, session)
    return f"Removed {resident}'s session “{session.title}”."


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
