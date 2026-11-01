"""Errands (once called session requests): the Mayor asks, the owner approves, Babo runs.

The Mayor doesn't do things on the box himself. When something needs doing
(install a package, run a build, look at a log), he asks for a session: one
resident, one task, and exactly the access it needs, with his reason. Asking
does nothing by itself.

The owner sees the request in their review (web or ``babo review``) word for word,
with what it adds beyond the resident's profile, and approves or declines it.
An approval counts only for the request as the owner saw it: the review sends
back a fingerprint of what was shown, and a request that changed in the
meantime is refused. Approved requests are run by the upkeep service: it
starts that resident's opencode session with exactly that access, sends the
task, has the resident review the session into its memory, freezes it, and
writes the result to the Mayor. A request whose file changed after the
approval was committed is not run.

Access is "allow" or "deny" per capability, plus exact shell commands. Nobody
is at a terminal while a request runs, so nothing is left to "ask": what the
approval doesn't allow is denied.

Requests are kept in ``state/requests/``, so the town's history shows every
request, decision and result.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from babo.baton import gitops, profiles
from babo.baton.inbox import deliver
from babo.baton.notes import first_line
from babo.baton.registry import Registry
from babo.baton.town import OWNER, BatonError, Town, atomic_write, now_iso, slugify

if TYPE_CHECKING:
    from babo.config.schema import Config

HARNESS = "baton-harness"
_LEVELS = ("allow", "deny")
MAX_COMMANDS = 10


@dataclass
class SessionRequest:
    id: str
    resident: str
    task: str
    reason: str
    access: dict[str, str] = field(default_factory=lambda: cast(dict[str, str], {}))
    commands: list[str] = field(default_factory=lambda: cast(list[str], []))
    requested_by: str = "mayor"
    created: str = ""
    status: str = "pending"  # pending | approved | declined | running | done | failed
    decided: str = ""
    decision_reason: str = ""
    approved_digest: str = ""
    session_key: str = ""  # the Mayor's chat it was asked from
    decided_by: str = ""  # the owner, or "allow-all" when that chat had allow-all on
    session_id: str = ""
    result: str = ""
    visibility: str = "private"  # private: the asker gets the resident's summary; public: its full output too

    def digest(self) -> str:
        """A fingerprint of what the owner is asked to approve."""
        shown = {"resident": self.resident, "task": self.task, "reason": self.reason,
                 "access": dict(sorted(self.access.items())), "commands": self.commands, "requested_by": self.requested_by}
        if self.visibility != "private":  # (requests from before visibility keep their fingerprint)
            shown["visibility"] = self.visibility
        return hashlib.sha256(json.dumps(shown, sort_keys=True).encode()).hexdigest()[:16]


def _folder(town: Town) -> Path:
    return town.state / "requests"


def _path(town: Town, request_id: str) -> Path:
    if not request_id or "/" in request_id or request_id.startswith("."):
        raise BatonError(f"'{request_id}' is not a request")
    return _folder(town) / f"{request_id}.json"


def save(town: Town, request: SessionRequest) -> Path:
    path = _path(town, request.id)
    atomic_write(path, json.dumps(asdict(request), indent=2, ensure_ascii=False) + "\n")
    return path


def load(town: Town, request_id: str) -> SessionRequest:
    path = _path(town, request_id)
    if not path.is_file():
        raise BatonError(f"There is no request '{request_id}'.")
    return SessionRequest(**json.loads(path.read_text(encoding="utf-8")))


def all_requests(town: Town) -> list[SessionRequest]:
    out: list[SessionRequest] = []
    for path in sorted(_folder(town).glob("*.json")) if _folder(town).exists() else []:
        try:
            out.append(SessionRequest(**json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError):
            continue
    return out


def pending(town: Town) -> list[SessionRequest]:
    return [r for r in all_requests(town) if r.status == "pending"]


# -- asking ----------------------------------------------------------------------

def create(town: Town, *, requested_by: str, resident: str, task: str, reason: str,
           access: dict[str, object] | None = None, commands: list[str] | None = None,
           session_key: str | None = None, visibility: str = "private") -> SessionRequest:
    """Record a request. It does nothing until the owner approves it, unless the owner
    turned allow-all on for the chat it comes from: then it is approved now."""
    from babo.baton import allow_all
    from babo.baton.opencode import runs_in_opencode

    target = Registry(town).resolve(resident)
    if target is None or target.status != "active":
        raise BatonError(f"No resident called '{resident}'. Ask for one of the town's residents, or post a move-in request for a newcomer.")
    if not runs_in_opencode(target.office):
        raise BatonError("The Mayor can't be given a session; ask for another resident to do it.")
    if not task.strip():
        raise BatonError("Say what the session should do (task).")
    if not reason.strip():
        raise BatonError(f"Say why it's needed (reason); {town.owner_name} reads it before deciding.")
    keys = {c.key for c in profiles.CAPABILITIES}
    clean: dict[str, str] = {}
    for key, raw in (access or {}).items():
        value = str(raw).strip().lower()
        if key not in keys:
            raise BatonError(f"Unknown access '{key}'. Access can set: {', '.join(sorted(keys))}.")
        if value not in _LEVELS:
            raise BatonError(f"Access for {key} must be allow or deny (nobody is at a terminal to answer 'ask').")
        clean[key] = value
    visibility = (visibility or "private").strip().lower()
    if visibility not in ("private", "public"):
        raise BatonError("visibility must be private (you get the resident's summary) or public (its full output too).")
    wanted = [str(c).strip() for c in commands or [] if str(c).strip()]
    if len(wanted) > MAX_COMMANDS:
        raise BatonError(f"Ask for at most {MAX_COMMANDS} commands; split the work into more requests.")
    if any("\n" in c for c in wanted):
        raise BatonError("Give each command on its own, one line each.")
    stamp = now_iso()
    request = SessionRequest(
        id=f"{stamp[:10]}-{slugify(target.id, 20)}-{secrets.token_hex(3)}", resident=target.id,
        task=task.strip(), reason=reason.strip(), access=clean, commands=wanted,
        requested_by=requested_by, created=stamp, session_key=session_key or "", visibility=visibility,
    )
    if allow_all.is_on(town, session_key):
        request.status = "approved"
        request.decided = stamp
        request.decided_by = "allow-all"
        request.decision_reason = f"Approved automatically: allow-all is on for {session_key}."
        request.approved_digest = request.digest()
    save(town, request)
    approved = " (approved automatically: allow-all)" if request.status == "approved" else ""
    gitops.commit_paths(town, [_path(town, request.id)], f"Errand from {requested_by}{approved}: {first_line(task, 60)}")
    return request


def recent(town: Town, *, limit: int = 10) -> list[SessionRequest]:
    """Requests that were decided, newest first: what ran, with or without the owner's review."""
    decided = [r for r in all_requests(town) if r.status != "pending"]
    return sorted(decided, key=lambda r: r.decided or r.created, reverse=True)[:limit]


# -- deciding ----------------------------------------------------------------------

def describe(town: Town, config: Config, request: SessionRequest) -> dict[str, Any]:
    """The request as the owner sees it in review: word for word, and what it adds beyond the resident's profile."""
    office = Registry(town).require_active(request.resident).office
    base = profiles.load(town, config.baton, request.resident, office)
    wider = [f"{c.label}: {base.level(c.key)} → allow" for c in profiles.CAPABILITIES
             if request.access.get(c.key) == "allow" and base.level(c.key) != "allow"]
    return {
        **asdict(request), "digest": request.digest(),
        "access_labels": {c.key: c.label for c in profiles.CAPABILITIES},
        "profile": {c.key: base.level(c.key) for c in profiles.CAPABILITIES},
        "wider": wider + ([f"Commands: {', '.join(request.commands)}"] if request.commands else []),
    }


def decide(town: Town, request_id: str, *, approve: bool, reason: str, digest: str | None = None, commit: bool = True) -> str:
    request = load(town, request_id)
    if request.status != "pending":
        raise BatonError(f"That request is already {request.status}.")
    if digest is not None and digest != request.digest():
        raise BatonError("That request changed after you opened it. Reload it and decide again.")
    request.decided = now_iso()
    request.decided_by = OWNER
    request.decision_reason = reason.strip() or "(no reason given)"
    if approve:
        request.status = "approved"
        request.approved_digest = request.digest()
        said = f"Errand approved: {request.resident} will run it as soon as upkeep picks it up (within a minute)."
    else:
        request.status = "declined"
        said = "Errand declined."
        _tell(town, request, OWNER, f"{town.owner_name} declined your request for {request.resident}: {first_line(request.task, 80)}\n\n"
                                    f"Reason: {request.decision_reason}")
    path = save(town, request)
    if commit:
        gitops.commit_paths(town, [path], f"{town.owner_name} {'approved' if approve else 'declined'} errand {request.id}")
    return said


def _tell(town: Town, request: SessionRequest, sender: str, text: str) -> None:
    if Registry(town).get(request.requested_by) is not None:
        deliver(town, request.requested_by, kind="reply", sender=sender, subject=f"errand {request.id}", text=text)


# -- running -----------------------------------------------------------------------

def next_approved(town: Town) -> SessionRequest | None:
    for request in all_requests(town):
        if request.status == "approved":
            return request
    return None


def recover_interrupted(town: Town) -> list[str]:
    """Requests left "running" when Babo stopped: mark them failed and tell whoever asked."""
    out: list[str] = []
    for request in all_requests(town):
        if request.status == "running":
            request.status = "failed"
            request.result = "Interrupted: Babo restarted while it ran. Ask again if it still needs doing."
            save(town, request)
            gitops.commit_paths(town, [_path(town, request.id)], f"Errand {request.id} interrupted")
            _tell(town, request, HARNESS, f"Your errand {request.id} for {request.resident} was interrupted: {request.result}")
            out.append(request.id)
    return out


def _unchanged_since_approval(town: Town, request: SessionRequest) -> bool:
    """The approved request is exactly what was approved: its fingerprint, and its file as committed."""
    if request.approved_digest != request.digest():
        return False
    path = _path(town, request.id)
    rel = str(path.relative_to(town.root))
    return gitops.git(town, "diff", "--quiet", "HEAD", "--", rel, check=False).returncode == 0


def task_message(town: Town, request: SessionRequest) -> str:
    lines = [
        f"[errand {request.id}, approved by {town.owner_name}] {request.task}",
        "",
        f"Why: {request.reason}",
        "Nobody is at a terminal while this runs: don't ask questions; do the task, then say plainly what you did and what came of it.",
    ]
    if request.commands:
        lines += ["", "You may run exactly these shell commands, written exactly like this (nothing else is allowed):"]
        lines += [f"    {c}" for c in request.commands]
    return "\n".join(lines)


def run(town: Town, config: Config, request: SessionRequest, *, config_path: Path) -> SessionRequest:
    """Run one approved request in its resident's opencode session, then tell the Mayor how it went."""
    from babo.baton import opencode

    if request.status != "approved":
        raise BatonError(f"Errand {request.id} is {request.status}, not approved.")
    if not _unchanged_since_approval(town, request):
        request.status = "failed"
        request.result = "Not run: the request changed after it was approved."
        save(town, request)
        gitops.commit_paths(town, [_path(town, request.id)], f"Errand {request.id} not run: changed after approval")
        _tell(town, request, HARNESS, f"Your errand {request.id} was not run: it changed after {town.owner_name} approved it. Ask again.")
        return request
    request.status = "running"
    save(town, request)
    gitops.commit_paths(town, [_path(town, request.id)], f"Errand {request.id} running")
    session = None
    output = ""
    try:
        session = opencode.new_session(
            town, config, request.resident, title=f"request: {first_line(request.task, 60)}",
            overrides=cast(dict[str, object], dict(request.access)), by=OWNER, kind=f"request:{request.id}",
            commands=request.commands,
        )
        request.session_id = session.id
        reply = opencode.send(town, config, session, task_message(town, request), config_path=config_path)
        output = opencode.public_output(town, opencode.get_session(town, request.resident, session.id)) if request.visibility == "public" else ""
        opencode.checkpoint(town, config, opencode.get_session(town, request.resident, session.id),
                            trigger="session_end", config_path=config_path)
        request.status = "done"
        request.result = reply or "(no reply)"
    except Exception as exc:  # the Mayor hears about any failure, in words
        request.status = "failed"
        request.result = f"{type(exc).__name__}: {exc}"
    finally:
        if session is not None:
            opencode.freeze(town, opencode.get_session(town, request.resident, session.id))
    save(town, request)
    gitops.commit_paths(town, [_path(town, request.id)], f"Errand {request.id} {request.status}")
    outcome = "finished" if request.status == "done" else "failed"
    # The result is the resident's own account of what it did.
    text = f"Your errand {request.id} for {request.resident} {outcome}: {first_line(request.task, 80)}\n\n{request.result[:4000]}"
    if output:  # a public session: what it did, step by step, as well as its own account
        text += f"\n\n## The session's full output (public)\n\n{output}"
    _tell(town, request, request.resident if request.status == "done" else HARNESS, text)
    return request
