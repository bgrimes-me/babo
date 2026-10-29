"""Markdown notes with YAML frontmatter: the unit of knowledge in the town."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from babo.baton.town import atomic_write, now_iso, slugify

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
NOTE_STATUSES = ("active", "superseded", "contested")


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    try:
        loaded: object = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(loaded, dict):
        return {}, text
    return cast(dict[str, Any], loaded), text[match.end():]


def join_frontmatter(meta: dict[str, Any], body: str) -> str:
    if not meta:
        return body
    dumped = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, default_flow_style=None).strip()
    return f"---\n{dumped}\n---\n\n{body.lstrip()}"


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in cast(list[object], value) if str(item).strip()]
    return [str(value)]


@dataclass
class Note:
    path: Path
    title: str
    body: str
    created: str = ""
    updated: str = ""
    authors: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    status: str = "active"
    supersedes: str | None = None
    aliases: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Note:
        text = path.read_text(encoding="utf-8")
        meta, body = split_frontmatter(text)
        title = str(meta.pop("title", "") or "")
        if not title:
            heading = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
            title = heading.group(1).strip() if heading else path.stem.replace("-", " ")
        status = str(meta.pop("status", "active") or "active")
        supersedes = meta.pop("supersedes", None)
        return cls(
            path=path,
            title=title,
            body=body,
            created=str(meta.pop("created", "") or ""),
            updated=str(meta.pop("updated", "") or ""),
            authors=_as_list(meta.pop("authors", None)),
            sources=_as_list(meta.pop("sources", None)),
            status=status if status in NOTE_STATUSES else "active",
            supersedes=str(supersedes) if supersedes else None,
            aliases=_as_list(meta.pop("aliases", None)),
            keywords=_as_list(meta.pop("keywords", None)),
            extra={k: v for k, v in meta.items() if k != "tags"},  # tags are derived from keywords
        )

    def render(self) -> str:
        meta: dict[str, Any] = {
            "title": self.title,
            "created": self.created or now_iso(),
            "updated": self.updated or now_iso(),
            "authors": self.authors,
            "sources": self.sources,
            "status": self.status,
        }
        if self.supersedes:
            meta["supersedes"] = self.supersedes
        meta["aliases"] = self.aliases
        meta["keywords"] = self.keywords
        # Obsidian's own property for grouping notes: the keywords, as tags (no spaces).
        meta["tags"] = [slugify(k) for k in self.keywords if slugify(k)]
        meta.update(self.extra)
        return join_frontmatter(meta, self.body)

    def save(self) -> None:
        atomic_write(self.path, self.render())


def note_path(directory: Path, title: str) -> Path:
    return directory / f"{slugify(title)}.md"


def write_note(
    directory: Path,
    *,
    title: str,
    body: str,
    author: str,
    aliases: list[str] | None = None,
    keywords: list[str] | None = None,
    sources: list[str] | None = None,
    status: str = "active",
    supersedes: str | None = None,
    path: Path | None = None,
) -> Note:
    """Create or update a note, keeping its creation time and author list."""
    target = path or note_path(directory, title)
    stamp = now_iso()
    if target.exists():
        note = Note.load(target)
        note.title = title or note.title
        note.body = body
        note.updated = stamp
        if author not in note.authors:
            note.authors.append(author)
        note.aliases = _merge(note.aliases, aliases)
        note.keywords = _merge(note.keywords, keywords)
        note.sources = _merge(note.sources, sources)
        note.status = status
        if supersedes:
            note.supersedes = supersedes
    else:
        note = Note(
            path=target,
            title=title,
            body=body,
            created=stamp,
            updated=stamp,
            authors=[author],
            sources=list(sources or []),
            status=status,
            supersedes=supersedes,
            aliases=list(aliases or []),
            keywords=list(keywords or []),
        )
    note.save()
    return note


def _merge(existing: list[str], new: list[str] | None) -> list[str]:
    out = list(existing)
    for item in new or []:
        item = str(item).strip()
        if item and item.lower() not in {value.lower() for value in out}:
            out.append(item)
    return out


def first_line(text: str, limit: int = 120) -> str:
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""
