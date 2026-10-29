"""Checkpoints and BATON.md.

Every checkpoint is appended to ``checkpoints/YYYY-MM-DD.jsonl`` (the record).
``BATON.md`` shows the latest checkpoint for each thread, newest first, and is
re-rendered from the record whenever a checkpoint is written.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from nanobot.baton.town import atomic_write, now_iso, today

LIST_FIELDS = ("done", "learned", "next", "blockers")


def _strs(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in cast(list[object], value)]
    return [str(value)] if value else []


@dataclass
class Checkpoint:
    thread: str
    doing: str = ""
    done: list[str] = field(default_factory=list)
    learned: list[str] = field(default_factory=list)
    next: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    confidence: float = 0.5
    # authored (by the resident) | mechanical (from the event log)
    source: str = "authored"
    trigger: str = ""
    resident: str = ""
    session: str = ""
    at: str = ""
    tool_calls: int = 0

    def to_record(self) -> dict[str, Any]:
        return {
            "at": self.at or now_iso(),
            "resident": self.resident,
            "session": self.session,
            "thread": self.thread,
            "doing": self.doing,
            **{name: list(getattr(self, name)) for name in LIST_FIELDS},
            "confidence": self.confidence,
            "source": self.source,
            "trigger": self.trigger,
            "tool_calls": self.tool_calls,
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Checkpoint:
        return cls(
            thread=str(record.get("thread") or "general"),
            doing=str(record.get("doing") or ""),
            done=_strs(record.get("done")),
            learned=_strs(record.get("learned")),
            next=_strs(record.get("next")),
            blockers=_strs(record.get("blockers")),
            confidence=float(record.get("confidence") or 0.0),
            source=str(record.get("source") or "authored"),
            trigger=str(record.get("trigger") or ""),
            resident=str(record.get("resident") or ""),
            session=str(record.get("session") or ""),
            at=str(record.get("at") or ""),
            tool_calls=int(record.get("tool_calls") or 0),
        )

    def render(self) -> str:
        how = "written by me" if self.source == "authored" else "mechanical record from the event log"
        lines = [f"## {self.thread}", f"_{self.at[:16].replace('T', ' ')} · {how} · confidence {self.confidence:.1f}_", ""]
        if self.doing:
            lines.append(f"**Doing:** {self.doing}")
        for name, label in (("done", "Done"), ("learned", "Learned"), ("next", "Next"), ("blockers", "Blockers")):
            items = getattr(self, name)
            if items:
                lines.append(f"**{label}:**")
                lines += [f"- {item}" for item in items]
        return "\n".join(lines).rstrip() + "\n"


def iter_checkpoints(resident_dir: Path) -> list[Checkpoint]:
    out: list[Checkpoint] = []
    for path in sorted((resident_dir / "checkpoints").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Checkpoint.from_record(json.loads(line)))
            except (ValueError, TypeError):
                continue
    out.sort(key=lambda c: c.at)
    return out


def latest_by_thread(resident_dir: Path) -> list[Checkpoint]:
    latest: dict[str, Checkpoint] = {}
    for cp in iter_checkpoints(resident_dir):
        latest[cp.thread] = cp
    return sorted(latest.values(), key=lambda c: c.at, reverse=True)


def current_entry(resident_dir: Path, thread: str | None = None) -> Checkpoint | None:
    entries = latest_by_thread(resident_dir)
    if thread:
        for cp in entries:
            if cp.thread == thread:
                return cp
    return entries[0] if entries else None


def render_baton(entries: list[Checkpoint]) -> str:
    if not entries:
        return "# Baton\n\nNo checkpoints yet.\n"
    body = "\n".join(cp.render() for cp in entries)
    return "# Baton\n\nMy latest checkpoint for each thread, newest first.\n\n" + body


def append_checkpoint(resident_dir: Path, cp: Checkpoint) -> Path:
    cp.at = cp.at or now_iso()
    path = resident_dir / "checkpoints" / f"{today()}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(cp.to_record(), ensure_ascii=False) + "\n")
    atomic_write(resident_dir / "BATON.md", render_baton(latest_by_thread(resident_dir)))
    return path
