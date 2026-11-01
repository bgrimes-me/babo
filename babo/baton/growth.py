"""How the town grows and changes: tools, proposals, founding, projects, notes.

Everything the owner decides in the weekly review is a function here, so the
review CLI stays a thin layer of plain prompts and every decision can be
tested. The owner's reason always goes to the resident concerned.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from babo.baton.founding import (
    approve_founding_request,
    decline_founding_request,
)
from babo.baton.inbox import deliver
from babo.baton.ledger import write_ledger
from babo.baton.notes import join_frontmatter, split_frontmatter
from babo.baton.registry import Registry
from babo.baton.tools.core import proposal_files
from babo.baton.town import OWNER, BatonError, Town, atomic_write, now_iso, slugify


def _meta(path: Path) -> tuple[dict[str, Any], str]:
    return split_frontmatter(path.read_text(encoding="utf-8"))


def _tell(town: Town, resident: str, subject: str, text: str) -> None:
    if Registry(town).get(resident) is not None:
        deliver(town, resident, kind="reply", sender=OWNER, subject=subject, text=text)


# -- notes from the owner -----------------------------------------------------------
def leave_note(town: Town, resident: str, text: str) -> Path:
    target = Registry(town).resolve(resident)
    if target is None:
        raise BatonError(f"No resident called '{resident}'.")
    return deliver(town, target.id, kind="note", sender=OWNER, text=text)


# -- tools ----------------------------------------------------------------------
@dataclass
class PendingTool:
    name: str
    folder: Path
    author: str
    description: str
    tested: bool | None
    isolation: str
    review: str


def pending_tools(town: Town) -> list[PendingTool]:
    out: list[PendingTool] = []
    for folder in sorted(p for p in (town.tools / "pending").glob("*") if p.is_dir()):
        meta, body = _meta(folder / "TOOL.md") if (folder / "TOOL.md").exists() else ({}, "")
        result = _meta(folder / "TEST-RESULT.md")[0] if (folder / "TEST-RESULT.md").exists() else {}
        review = _meta(folder / "REVIEW.md")[1].strip() if (folder / "REVIEW.md").exists() else ""
        out.append(PendingTool(
            name=folder.name, folder=folder, author=str(meta.get("author", "?")),
            description=str(meta.get("description") or body.strip()[:140]),
            tested=cast(bool, result["passed"]) if "passed" in result else None,
            isolation=str(result.get("isolation", "")), review=review,
        ))
    return out


def decide_tool(town: Town, name: str, *, approve: bool, reason: str) -> Path:
    folder = town.tools / "pending" / name
    if not folder.is_dir():
        raise BatonError(f"No pending tool called {name}.")
    meta, body = _meta(folder / "TOOL.md")
    if approve:
        result = _meta(folder / "TEST-RESULT.md")[0] if (folder / "TEST-RESULT.md").exists() else {}
        if result.get("passed") is not True:
            raise BatonError(f"{name} has not passed its isolated test; it can't be approved yet.")
    state = "active" if approve else "rejected"
    meta.update({"status": state, "decided": now_iso(), "decided_by": OWNER, "decision_reason": reason})
    atomic_write(folder / "TOOL.md", join_frontmatter(meta, body))
    dest = town.tools / state / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(folder), str(dest))
    write_ledger(town)
    verdict = "approved: it is now active for every resident" if approve else "not approved"
    _tell(town, str(meta.get("author", "")), f"tool {name}: {state}", f"{town.owner_name}'s decision on your tool `{name}`: {verdict}.\n\nReason: {reason}")
    return dest


# -- proposals ---------------------------------------------------------------------
@dataclass
class OpenProposal:
    path: Path
    title: str
    author: str
    project: str
    review: str
    town_name: str = ""


def open_proposals(town: Town) -> list[OpenProposal]:
    out: list[OpenProposal] = []
    for path in proposal_files(town.proposals / "open"):
        meta, _ = _meta(path)
        review_path = path.with_name(path.stem + ".review.md")
        review = _meta(review_path)[1].strip() if review_path.exists() else ""
        out.append(OpenProposal(path, str(meta.get("title", path.stem)), str(meta.get("author", "?")), str(meta.get("project") or ""), review,
                                 str(meta.get("town_name") or "")))
    return out


def decide_proposal(town: Town, path: Path, *, accept: bool, reason: str) -> Path:
    meta, body = _meta(path)
    state = "accepted" if accept else "declined"
    meta.update({"status": state, "decided": now_iso(), "decided_by": OWNER, "decision_reason": reason})
    dest = town.proposals / state / path.name
    atomic_write(dest, join_frontmatter(meta, body))
    path.unlink()
    review = path.with_name(path.stem + ".review.md")
    if review.exists():
        shutil.move(str(review), str(dest.with_name(review.name)))
    extra = ""
    project = str(meta.get("project") or "")
    if accept and project:
        bare = promote_project(town, str(meta.get("author", "")), project)
        extra = f"\n\nThe project now lives in the commons ({bare.relative_to(town.root)}); every resident has a working copy in their projects/."
    new_name = str(meta.get("town_name") or "")
    if accept and new_name:
        named = town.set_name(new_name, by=f"proposal by {meta.get('author', '?')}")
        extra += f"\n\nThe town is now called {named}."
    _tell(town, str(meta.get("author", "")), f"proposal {state}: {meta.get('title', '')}",
          f"{town.owner_name} {state} your proposal \"{meta.get('title', '')}\".\n\nReason: {reason}{extra}")
    return dest


# -- founding requests ---------------------------------------------------------------
def decide_founding(town: Town, request: Path, *, approve: bool, reason: str) -> str:
    meta, _ = _meta(request)
    author = str(meta.get("author", "mayor"))
    if approve:
        resident = approve_founding_request(town, request, approved_by=OWNER, note=reason)
        _tell(town, author, f"move-in approved: {resident.id}",
              f"{town.owner_name} approved `{resident.id}` moving in (office: {resident.office}).\n\nReason: {reason}")
        return resident.id
    decline_founding_request(town, request, declined_by=OWNER, reason=reason)
    _tell(town, author, "move-in declined", f"{town.owner_name} declined the move-in request {request.name}.\n\nReason: {reason}")
    return ""


# -- flags ---------------------------------------------------------------------------
def _flag_rows(town: Town) -> list[dict[str, Any]]:
    path = town.state / "flags.jsonl"
    if not path.exists():
        return []
    return [cast(dict[str, Any], json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def stale_flags(town: Town, *, days: int = 7, now: datetime | None = None) -> list[dict[str, Any]]:
    cutoff = (now or datetime.now().astimezone()) - timedelta(days=days)
    out: list[dict[str, Any]] = []
    for row in _flag_rows(town):
        if row.get("resolved"):
            continue
        try:
            at = datetime.fromisoformat(str(row.get("at")))
        except ValueError:
            continue
        if at <= cutoff:
            out.append(row)
    return out


def resolve_flag(town: Town, flag_path: str, *, note: str = "") -> None:
    rows = _flag_rows(town)
    for row in rows:
        if row.get("path") == flag_path:
            row.update({"resolved": True, "resolved_at": now_iso(), "resolved_note": note})
    atomic_write(town.state / "flags.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


# -- projects --------------------------------------------------------------------------
def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "user.name=Baton", "-c", "user.email=baton@town.local", *args],
        cwd=cwd, capture_output=True, text=True, timeout=120,
    )


def promote_project(town: Town, resident: str, name: str) -> Path:
    """Move a resident's project to the commons. The commons holds the shared
    repository; every resident (the author included) works in its own clone
    and pushes back, so private spaces stay private."""
    slug = slugify(name)
    source = town.resident_dir(resident) / "projects" / slug
    if not source.is_dir():
        raise BatonError(f"{resident} has no project called {slug}.")
    bare = town.commons_projects / f"{slug}.git"
    if bare.exists():
        raise BatonError(f"The commons already has a project called {slug}.")
    if not (source / ".git").exists():
        _git(source, "init", "-q", "-b", "main")
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", f"Promote {slug} to the commons")
    town.commons_projects.mkdir(parents=True, exist_ok=True)
    result = _git(town.root, "clone", "-q", "--bare", str(source), str(bare))
    if result.returncode != 0:
        raise BatonError(f"Could not create the commons repository: {result.stderr.strip()}")
    _git(source, "remote", "remove", "origin")
    _git(source, "remote", "add", "origin", str(bare))
    for r in Registry(town).all():
        if r.status == "active":
            ensure_project_clones(town, r.id)
    return bare


def ensure_project_clones(town: Town, resident: str) -> list[str]:
    """Give a resident a working copy of every commons project it lacks."""
    made: list[str] = []
    projects = town.resident_dir(resident) / "projects"
    for bare in sorted(town.commons_projects.glob("*.git")):
        target = projects / bare.name[:-4]
        if target.exists():
            continue
        projects.mkdir(parents=True, exist_ok=True)
        if _git(projects, "clone", "-q", str(bare), target.name).returncode == 0:
            made.append(target.name)
    return made
