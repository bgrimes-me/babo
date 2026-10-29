"""Inboxes: letters, flags, Squid's notes and a resident's own remember() notes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from babo.baton.notes import first_line, join_frontmatter, split_frontmatter
from babo.baton.town import Town, atomic_write, now_iso, slugify

# Kinds shown as "unread" at session start. remember() notes are the
# resident's own and are folded at the next memory review instead.
ANNOUNCED_KINDS = ("letter", "flag", "note", "reply")
KINDS = (*ANNOUNCED_KINDS, "remember")


@dataclass(frozen=True)
class InboxItem:
    path: Path
    kind: str
    sender: str
    created: str
    read: bool
    folded: bool
    subject: str
    body: str


def deliver(town: Town, to: str, *, kind: str, sender: str, text: str, subject: str = "", extra: dict[str, Any] | None = None) -> Path:
    """Write one item into ``to``'s inbox. The caller has already checked that
    ``to`` is a resident; this writes only inside ``residents/<to>/inbox``."""
    if kind not in KINDS:
        raise ValueError(f"unknown inbox kind {kind}")
    inbox = town.resident_dir(to) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    subject = subject or first_line(text, 60) or kind
    path = inbox / f"{stamp}-{kind}-{slugify(sender, 20)}-{slugify(subject, 30)}.md"
    meta = {"kind": kind, "from": sender, "to": to, "created": now_iso(), "subject": subject, "read": False}
    if kind == "remember":
        meta["folded"] = False
    meta.update(extra or {})
    atomic_write(path, join_frontmatter(meta, text.strip() + "\n"))
    return path


def load_item(path: Path) -> InboxItem:
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    return InboxItem(
        path=path,
        kind=str(meta.get("kind") or "letter"),
        sender=str(meta.get("from") or "?"),
        created=str(meta.get("created") or ""),
        read=bool(meta.get("read")),
        folded=bool(meta.get("folded")),
        subject=str(meta.get("subject") or first_line(body, 60)),
        body=body,
    )


def items(town: Town, resident_id: str) -> list[InboxItem]:
    inbox = town.resident_dir(resident_id) / "inbox"
    return [load_item(p) for p in sorted(inbox.glob("*.md"))]


def unread(town: Town, resident_id: str) -> list[InboxItem]:
    return [i for i in items(town, resident_id) if i.kind in ANNOUNCED_KINDS and not i.read]


def set_flag(path: Path, **changes: object) -> None:
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    if all(meta.get(k) == v for k, v in changes.items()):
        return
    meta.update(changes)
    atomic_write(path, join_frontmatter(meta, body))
