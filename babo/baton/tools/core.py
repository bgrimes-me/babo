"""The Baton tools, implemented once.

``Toolbox`` is bound to one resident. Identity comes from the binding, never
from a tool argument, so a resident can only act as itself. The babo
adapter (in-process, for residents) and the MCP server (stdio, for OpenCode)
both call these methods.

Every method returns a short plain-text result written for a small model.
Errors raise ``BatonError`` with a message that says what to do instead.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from babo.baton import gitops
from babo.baton.inbox import deliver, set_flag
from babo.baton.notes import Note, first_line, join_frontmatter, split_frontmatter, write_note
from babo.baton.registry import UNCHOSEN, Registry, Resident
from babo.baton.scope import Scope
from babo.baton.search import index_file, open_index
from babo.baton.tokens import trim_to_tokens
from babo.baton.town import BatonError, Town, atomic_write, now_iso, slugify, today

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z' -]{0,39}$")
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
BOARD_KINDS = ("open_role", "request", "founding_request")
READ_NOTE_MAX_TOKENS = 2500


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    offices: tuple[str, ...] = ()  # empty: every resident
    read_only: bool = False


def _str(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


def _list(desc: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": desc}


TOOL_SPECS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            "search_memory",
            "Search your own notes and the commons. Returns the best matches with their paths.",
            {
                "type": "object",
                "properties": {
                    "query": _str("What to look for, in plain words."),
                    "k": {"type": "integer", "minimum": 1, "maximum": 10, "description": "How many results (default 3)."},
                    "scope": {"type": "string", "enum": ["both", "self", "commons"], "description": "Where to search (default both)."},
                },
                "required": ["query"],
            },
            read_only=True,
        ),
        ToolSpec(
            "read_note",
            "Read a note: your files (knowledge/x.md, inbox/...), the commons (commons/...), CHARTER.md, HOUSE.md, or a skill (skill:<name>).",
            {"type": "object", "properties": {"path": _str("Path of the note.")}, "required": ["path"]},
            read_only=True,
        ),
        ToolSpec(
            "remember",
            "Save a short note for yourself. It goes to your inbox and is folded into your memory at your next memory review.",
            {"type": "object", "properties": {"text": _str("What to remember.")}, "required": ["text"]},
        ),
        ToolSpec(
            "revise_self",
            "Rewrite your SELF.md (your own words about yourself). Optionally record a name you have chosen.",
            {
                "type": "object",
                "properties": {
                    "new_text": _str("The full new text of SELF.md."),
                    "reason": _str("Why you are revising it, in a few words."),
                    "name": _str("Optional: a name you have chosen for yourself."),
                },
                "required": ["new_text", "reason"],
            },
        ),
        ToolSpec(
            "send_letter",
            "Send a letter to another resident (by id or name). It arrives in their inbox.",
            {"type": "object", "properties": {"to": _str("Resident id or name."), "text": _str("The letter.")}, "required": ["to", "text"]},
        ),
        ToolSpec(
            "propose_tool",
            "Propose a new tool for the town. Give Python code defining run(input: str) -> str and a pytest test. It is tested in isolation, reviewed by the Inspector, and needs the owner's approval.",
            {
                "type": "object",
                "properties": {
                    "name": _str("Tool name: lowercase letters, digits, underscores."),
                    "code": _str("Python source defining run(input: str) -> str."),
                    "test": _str("pytest source that imports run from tool and tests it."),
                    "description": _str("One line: what the tool does."),
                },
                "required": ["name", "code", "test", "description"],
            },
        ),
        ToolSpec(
            "propose_change",
            "Propose a change to how the town or the harness works. The owner accepts or declines with a reason.",
            {
                "type": "object",
                "properties": {
                    "title": _str("Short title."),
                    "rationale": _str("Why this change is needed."),
                    "description": _str("What should change."),
                    "project": _str("Optional: the name of one of your projects, to propose moving it to the commons."),
                    "town_name": _str("Optional: a name for the town. If the owner accepts, the town takes this name."),
                },
                "required": ["title", "rationale", "description"],
            },
        ),
        ToolSpec(
            "post_board",
            "Post to the town notice board: an open role, a request, or a founding request for a new resident (the owner approves).",
            {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(BOARD_KINDS)},
                    "text": _str("The post. For a founding request, describe the office and why it is needed."),
                    "office": _str("For a founding request: short office name, e.g. gardener."),
                },
                "required": ["kind", "text"],
            },
            offices=("mayor",),
        ),
        ToolSpec(
            "request_session",
            "Ask the owner to run a task in another resident's session, with the access it needs (for example to install a package or run a build). "
            "Nothing happens until the owner approves; you get a letter with the result. Ask for the least access that will do: "
            "prefer exact commands over turning the shell on. Commands run without a terminal, so give flags like --noconfirm or -y; "
            "sudo works only if the machine allows it without a password. "
            "A session is private unless you ask for it to be public: from a private one you get only the resident's own summary "
            "of what it did. If you need to see the output yourself (a command's result, what it found, files it made), ask for "
            "visibility public.",
            {
                "type": "object",
                "properties": {
                    "resident": _str("Which resident does it (not yourself)."),
                    "task": _str("What the session should do, plainly and completely."),
                    "reason": _str("Why it's needed. The owner reads this before deciding."),
                    "access": {
                        "type": "object",
                        "description": "Access beyond the resident's profile, each allow or deny: files, shell, subagents, web_fetch, web_search, image_generation.",
                        "properties": {k: {"type": "string", "enum": ["allow", "deny"]}
                                       for k in ("files", "shell", "subagents", "web_fetch", "web_search", "image_generation")},
                    },
                    "commands": _list("Exact shell commands the session may run, each written exactly as it will be run."),
                    "visibility": {"type": "string", "enum": ["private", "public"],
                                   "description": "private (the default): you get the resident's summary. public: you also get its full output."},
                },
                "required": ["resident", "task", "reason"],
            },
            offices=("mayor",),
        ),
        ToolSpec(
            "commons_write",
            "Write or update a commons knowledge note. Add aliases and keywords so others find it.",
            {
                "type": "object",
                "properties": {
                    "title": _str("Note title."),
                    "body": _str("Markdown body."),
                    "aliases": _list("Other names for this topic."),
                    "keywords": _list("Search keywords."),
                    "authors": _list("Resident ids whose notes this came from."),
                    "sources": _list("Where the knowledge came from (paths or links)."),
                },
                "required": ["title", "body"],
            },
            offices=("archivist",),
        ),
        ToolSpec(
            "commons_supersede",
            "Replace an outdated commons note with a new one. The old note stays, marked superseded.",
            {
                "type": "object",
                "properties": {
                    "old_path": _str("Path of the outdated note, e.g. commons/knowledge/x.md."),
                    "title": _str("Title of the new note."),
                    "body": _str("Body of the new note."),
                    "reason": _str("Why the old note is outdated."),
                    "aliases": _list("Other names for this topic."),
                    "keywords": _list("Search keywords."),
                },
                "required": ["old_path", "title", "body", "reason"],
            },
            offices=("archivist",),
        ),
        ToolSpec(
            "commons_contest",
            "Mark a commons note as contested, keeping both views and their authors. The authors get a letter.",
            {
                "type": "object",
                "properties": {
                    "path": _str("Path of the commons note."),
                    "other_view": _str("The conflicting view, in full."),
                    "other_author": _str("Resident id who holds the other view."),
                    "note": _str("Optional: what would settle it."),
                },
                "required": ["path", "other_view", "other_author"],
            },
            offices=("archivist",),
        ),
        ToolSpec(
            "file_flag",
            "File a flag to a resident when their checkpoint claim and the event log disagree. Include the evidence.",
            {
                "type": "object",
                "properties": {
                    "resident": _str("Resident id."),
                    "claim": _str("What the checkpoint claimed."),
                    "evidence": _str("What the event log shows."),
                },
                "required": ["resident", "claim", "evidence"],
            },
            offices=("inspector",),
        ),
        ToolSpec(
            "review_item",
            "Attach your review to a pending tool (commons/tools/pending/<name>) or an open proposal (commons/proposals/open/<file>).",
            {
                "type": "object",
                "properties": {"path": _str("Path of the tool or proposal."), "review": _str("Your review: risks, test results, recommendation.")},
                "required": ["path", "review"],
            },
            offices=("inspector",),
        ),
    )
}


@dataclass
class Toolbox:
    town: Town
    resident_id: str
    index_path: Path
    # Called after a write that changes indexed knowledge (tests hook it).
    on_write: Callable[[Path], None] | None = None
    # Extra tool implementations (checkpoint, visit, commons tools) registered by later phases.
    extra: dict[str, Callable[..., Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.registry = Registry(self.town)
        self.scope = Scope(self.town, self.resident_id)

    # -- helpers ---------------------------------------------------------
    @property
    def me(self) -> Resident:
        return self.registry.require_active(self.resident_id)

    @property
    def home(self) -> Path:
        return self.town.resident_dir(self.resident_id)

    def _require_office(self, *offices: str) -> None:
        if "archivist" in offices and self.curates_for_archivist():
            return
        if self.me.office not in offices:
            raise BatonError(f"Blocked: only the {' or '.join(offices)} can do this; it is their lane. To get it done, send_letter to the {offices[0]} and say what you need.")

    def _indexed(self, path: Path) -> None:
        index_file(self.town, self.index_path, path)
        if self.on_write:
            self.on_write(path)

    def _recipient(self, who: str) -> Resident:
        resident = self.registry.resolve(who)
        if resident is None or resident.status != "active":
            known = ", ".join(r.display for r in self.registry.all() if r.status == "active")
            raise BatonError(f"No resident called '{who}'. Residents: {known}.")
        return resident

    def available(self) -> list[str]:
        office = self.me.office
        offices: set[str] = {office, "archivist"} if self.curates_for_archivist() else {office}
        names = [s.name for s in TOOL_SPECS.values() if not s.offices or offices & set(s.offices)]
        return names + list(self.extra)

    def curates_for_archivist(self) -> bool:
        """The Mayor keeps the commons while the town has no Archivist."""
        return self.me.office == "mayor" and not any(
            r.office == "archivist" and r.status == "active" for r in self.registry.all())

    def call(self, name: str, /, **kwargs: object) -> str:
        """Run a tool by name. ``name`` is positional-only because a tool may
        have an argument called ``name`` (propose_tool, revise_self)."""
        if name in self.extra:
            return self.extra[name](**kwargs)
        spec = TOOL_SPECS.get(name)
        if spec is None:
            raise BatonError(f"There is no tool called {name}. Your tools: {', '.join(self.available())}.")
        if spec.offices:
            self._require_office(*spec.offices)
        return getattr(self, name)(**kwargs)

    # -- everyone --------------------------------------------------------
    def search_memory(self, query: str, k: int = 3, scope: str = "both") -> str:
        owners = {"both": ("commons", self.resident_id), "self": (self.resident_id,), "commons": ("commons",)}.get(scope)
        if owners is None:
            raise BatonError("scope must be \"both\", \"self\" or \"commons\". Leave it out to search both.")
        hits = open_index(self.index_path).search(query, owners=owners, k=max(1, min(int(k), 10)))
        if not hits:
            return f"No notes match '{query}'. Try other words, or scope=both."
        lines: list[str] = []
        for hit in hits:
            where = "commons" if hit.owner == "commons" else "mine"
            flag = f" [{hit.status}]" if hit.status != "active" else ""
            lines.append(f"- {hit.title}{flag} ({where}) path: {hit.path}\n  {hit.snippet.strip()}")
        return "\n".join(lines)

    def read_note(self, path: str) -> str:
        target = self.scope.resolve_ref(path)
        text = target.read_text(encoding="utf-8")
        if target.parent == (self.home / "inbox").resolve() or target.parent == self.home / "inbox":
            set_flag(target, read=True)
        return trim_to_tokens(text, READ_NOTE_MAX_TOKENS, "\n[…the note continues; ask for a section if you need more]")

    def remember(self, text: str) -> str:
        if not text.strip():
            raise BatonError("Nothing to remember: text is empty.")
        deliver(self.town, self.resident_id, kind="remember", sender=self.resident_id, text=text)
        return "Saved. It will be folded into your memory at your next memory review."

    def revise_self(self, new_text: str, reason: str, name: str | None = None) -> str:
        if not new_text.strip():
            raise BatonError("new_text is empty. SELF.md was not changed.")
        changed: list[Path] = []
        message = f"{self.resident_id}: revise SELF ({reason.strip()[:80] or 'no reason given'})"
        if name:
            name = name.strip()
            if not _NAME_RE.match(name) or name.lower() == UNCHOSEN:
                raise BatonError("A name is 1-40 letters, spaces, hyphens or apostrophes.")
            for other in self.registry.all():
                if other.id != self.resident_id and other.name.lower() == name.lower():
                    raise BatonError(f"{other.id} is already called {name}.")
            self.registry.update(self.resident_id, name=name)
            changed += [self.town.registry_json, self.town.registry_md]
            message += f"; chose the name {name}"
        atomic_write(self.home / "SELF.md", new_text.rstrip() + "\n")
        changed.append(self.home / "SELF.md")
        author = (self.me.name if self.me.name != UNCHOSEN else self.resident_id, f"{self.resident_id}@town.local")
        gitops.commit_paths(self.town, changed, message, author=author)
        return "SELF.md revised and committed." + (f" Your name is now recorded as {name}." if name else "")

    def send_letter(self, to: str, text: str) -> str:
        recipient = self._recipient(to)
        if recipient.id == self.resident_id:
            raise BatonError("That's you. Use remember() for notes to yourself.")
        if not text.strip():
            raise BatonError("The letter is empty.")
        deliver(self.town, recipient.id, kind="letter", sender=self.resident_id, text=text)
        return f"Letter delivered to {recipient.display}."

    def propose_tool(self, name: str, code: str, test: str, description: str) -> str:
        name = name.strip()
        if not _TOOL_NAME_RE.match(name):
            raise BatonError("Tool names are lowercase letters, digits and underscores, 2-40 characters, starting with a letter.")
        for state in ("pending", "active"):
            if (self.town.tools / state / name).exists():
                raise BatonError(f"A tool called {name} is already {state}. Pick another name, or use the existing tool.")
        if "def run" not in code:
            raise BatonError("Not filed: the code must define run(input: str) -> str. Add that function and propose it again.")
        folder = self.town.tools / "pending" / name
        folder.mkdir(parents=True)
        atomic_write(folder / "tool.py", code.rstrip() + "\n")
        atomic_write(folder / "test_tool.py", test.rstrip() + "\n")
        meta = {"name": name, "description": first_line(description, 140), "author": self.resident_id, "created": now_iso(), "status": "pending"}
        atomic_write(folder / "TOOL.md", join_frontmatter(meta, description.strip() + "\n"))
        return f"Proposed tool {name}. It will be tested in isolation, reviewed by the Inspector, and then goes to {self.town.owner_name}."

    def propose_change(self, title: str, rationale: str, description: str, project: str | None = None, town_name: str | None = None) -> str:
        town_name = " ".join((town_name or "").split())
        if len(town_name) > 60:
            raise BatonError("Not filed: a town name is at most 60 characters. Shorten it and propose again.")
        if project:
            slug = slugify(project)
            if not (self.home / "projects" / slug).is_dir():
                raise BatonError(f"You have no project called {slug} in projects/. Check the folder name under projects/, or leave project out.")
            project = slug
        path = self.town.proposals / "open" / f"{today()}-{slugify(title)}.md"
        if path.exists():
            path = path.with_name(f"{path.stem}-{self.resident_id}.md")
        meta: dict[str, Any] = {"title": title.strip(), "author": self.resident_id, "created": now_iso(), "status": "open"}
        if project:
            meta["project"] = project
        if town_name:
            meta["town_name"] = town_name
        body = f"# {title.strip()}\n\n## Why\n\n{rationale.strip()}\n\n## What\n\n{description.strip()}\n"
        atomic_write(path, join_frontmatter(meta, body))
        return f"Proposal filed as {self.scope.display(path)}. {self.town.owner_name} will accept or decline it with a reason."

    # -- mayor -----------------------------------------------------------
    def post_board(self, kind: str, text: str, office: str | None = None) -> str:
        if kind not in BOARD_KINDS:
            raise BatonError(f"kind must be one of {', '.join(BOARD_KINDS)}. Use one of those and post again.")
        meta: dict[str, object] = {"kind": kind, "author": self.resident_id, "created": now_iso(), "status": "open"}
        if kind == "founding_request":
            if not office or not office.strip():
                raise BatonError("A founding request needs an office name (e.g. gardener).")
            meta["office"] = slugify(office, 28)
        path = self.town.board / f"{today()}-{kind}-{slugify(office or first_line(text, 40))}.md"
        n = 2
        while path.exists():
            path = path.with_name(f"{path.stem}-{n}.md")
            n += 1
        atomic_write(path, join_frontmatter(meta, text.strip() + "\n"))
        if kind == "founding_request":
            return f"Founding request posted ({self.scope.display(path)}). {self.town.owner_name} decides in the weekly review."
        return f"Posted to the board: {self.scope.display(path)}."

    def request_session(self, resident: str, task: str, reason: str, access: dict[str, object] | None = None,
                        commands: list[str] | None = None, visibility: str = "private") -> str:
        from babo.agent.tools.context import current_request_session_key
        from babo.baton import requests

        request = requests.create(self.town, requested_by=self.resident_id, resident=resident, task=task, reason=reason,
                                  access=access, commands=commands, session_key=current_request_session_key(),
                                  visibility=visibility)
        if request.status == "approved":
            return (f"Request {request.id} was approved automatically ({self.town.owner_name} turned allow-all on for this chat). "
                    "It starts within a minute; you'll get a letter with the result.")
        return (f"Request {request.id} is waiting for {self.town.owner_name}'s approval. Nothing runs until it is approved; "
                "you'll get a letter with the result, or with the reason if it is declined.")

    # -- archivist -------------------------------------------------------
    def _commons_path(self, ref: str) -> Path:
        path = self.scope.resolve_ref(ref)
        try:
            path.resolve().relative_to(self.town.knowledge.resolve())
        except ValueError as exc:
            raise BatonError("That is not a commons knowledge note (commons/knowledge/...).") from exc
        return path

    def commons_write(
        self,
        title: str,
        body: str,
        aliases: list[str] | None = None,
        keywords: list[str] | None = None,
        authors: list[str] | None = None,
        sources: list[str] | None = None,
    ) -> str:
        note = write_note(
            self.town.knowledge, title=title, body=body, author=self.resident_id,
            aliases=aliases, keywords=keywords, sources=sources,
        )
        for author in authors or []:
            if author and author not in note.authors:
                note.authors.append(author)
        note.save()
        self._indexed(note.path)
        return f"Wrote {self.scope.display(note.path)}."

    def commons_supersede(
        self,
        old_path: str,
        title: str,
        body: str,
        reason: str,
        aliases: list[str] | None = None,
        keywords: list[str] | None = None,
    ) -> str:
        old = Note.load(self._commons_path(old_path))
        new_path = self.town.knowledge / f"{slugify(title)}.md"
        if new_path.resolve() == old.path.resolve():
            new_path = self.town.knowledge / f"{slugify(title)}-{today()}.md"
        rel_old = str(old.path.resolve().relative_to(self.town.root))
        new = write_note(
            self.town.knowledge, title=title, body=body, author=self.resident_id, aliases=aliases,
            keywords=keywords, sources=[rel_old], supersedes=rel_old, path=new_path,
        )
        old.status = "superseded"
        old.extra["superseded_by"] = str(new.path.resolve().relative_to(self.town.root))
        old.extra["superseded_reason"] = reason.strip()
        old.updated = now_iso()
        old.save()
        self._indexed(old.path)
        self._indexed(new.path)
        return f"Superseded {rel_old} with {self.scope.display(new.path)}."

    def commons_contest(self, path: str, other_view: str, other_author: str, note: str | None = None) -> str:
        target = Note.load(self._commons_path(path))
        holder = self._recipient(other_author)
        target.status = "contested"
        if holder.id not in target.authors:
            target.authors.append(holder.id)
        section = f"\n\n## Contested view (held by {holder.id})\n\n{other_view.strip()}\n"
        if note:
            section += f"\n_What would settle it: {note.strip()}_\n"
        target.body = target.body.rstrip() + section
        target.updated = now_iso()
        target.save()
        self._indexed(target.path)
        rel = str(target.path.resolve().relative_to(self.town.root))
        notified: list[str] = []
        for author in target.authors:
            resident = self.registry.get(author)
            if resident and resident.id != self.resident_id and resident.status == "active":
                deliver(
                    self.town, resident.id, kind="letter", sender=self.resident_id,
                    subject=f"contested: {target.title}",
                    text=f"The commons note {rel} now holds two views and is marked contested. Both views and their authors are kept. "
                    + (f"What would settle it: {note}" if note else "Reply if you can settle it."),
                )
                notified.append(resident.id)
        return f"Marked {rel} contested; wrote to {', '.join(notified) or 'nobody'}."

    # -- inspector -------------------------------------------------------
    def file_flag(self, resident: str, claim: str, evidence: str) -> str:
        target = self._recipient(resident)
        text = f"**Claim:** {claim.strip()}\n\n**Event log:** {evidence.strip()}\n\nThis is a note from the Inspector, not a verdict. Reply if the log is missing context."
        path = deliver(self.town, target.id, kind="flag", sender=self.resident_id, text=text, subject=f"flag: {first_line(claim, 50)}")
        record = self.town.state / "flags.jsonl"
        record.parent.mkdir(parents=True, exist_ok=True)
        with record.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": now_iso(), "to": target.id, "by": self.resident_id, "claim": claim, "path": str(path.relative_to(self.town.root)), "resolved": False}) + "\n")
        return f"Flag delivered to {target.display}."

    def review_item(self, path: str, review: str) -> str:
        target = (self.town.root / path.strip().strip("/")).resolve()
        pending = (self.town.tools / "pending").resolve()
        open_props = (self.town.proposals / "open").resolve()
        if target.parent == pending and target.is_dir():
            out = target / "REVIEW.md"
        elif target.parent.parent == pending and target.parent.is_dir():
            out = target.parent / "REVIEW.md"
        elif target.parent == open_props and target.suffix == ".md" and target.is_file() and not target.name.endswith(".review.md"):
            out = target.with_name(target.stem + ".review.md")
        else:
            raise BatonError("Review a pending tool (commons/tools/pending/<name>) or an open proposal (commons/proposals/open/<file>.md).")
        meta = {"reviewer": self.resident_id, "created": now_iso()}
        atomic_write(out, join_frontmatter(meta, review.strip() + "\n"))
        return f"Review saved to {out.relative_to(self.town.root)}."


def proposal_files(directory: Path) -> list[Path]:
    return [p for p in sorted(directory.glob("*.md")) if not p.name.endswith(".review.md")]


def read_meta(path: Path) -> dict[str, Any]:
    return split_frontmatter(path.read_text(encoding="utf-8"))[0]
