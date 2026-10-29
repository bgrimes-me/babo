"""LEDGER.md: what the town can do, one line per skill and active tool."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nanobot.baton.notes import first_line, split_frontmatter
from nanobot.baton.town import Town, atomic_write


@dataclass(frozen=True)
class Capability:
    kind: str  # skill | tool
    name: str
    description: str
    path: Path


def _read_meta(path: Path) -> tuple[dict[str, Any], str]:
    try:
        return split_frontmatter(path.read_text(encoding="utf-8"))
    except OSError:
        return {}, ""


def skills(town: Town) -> list[Capability]:
    out: list[Capability] = []
    for skill_md in sorted(town.skills.glob("*/SKILL.md")):
        meta, body = _read_meta(skill_md)
        name = str(meta.get("name") or skill_md.parent.name)
        desc = str(meta.get("description") or first_line(body))
        out.append(Capability("skill", name, desc, skill_md))
    return out


def active_tools(town: Town) -> list[Capability]:
    out: list[Capability] = []
    for tool_md in sorted((town.tools / "active").glob("*/TOOL.md")):
        meta, body = _read_meta(tool_md)
        name = str(meta.get("name") or tool_md.parent.name)
        desc = str(meta.get("description") or first_line(body))
        out.append(Capability("tool", name, desc, tool_md))
    return out


def render_ledger(town: Town) -> str:
    lines = ["# Ledger", "", "What the town can do. Generated nightly; read a skill with `read_note(\"skill:<name>\")`.", ""]
    sk = skills(town)
    tl = active_tools(town)
    lines += ["## Skills", ""]
    lines += [f"- {c.name}: {c.description}" for c in sk] or ["- (none yet)"]
    lines += ["", "## Tools", ""]
    lines += [f"- {c.name}: {c.description}" for c in tl] or ["- (none yet)"]
    return "\n".join(lines) + "\n"


def write_ledger(town: Town) -> None:
    atomic_write(town.ledger, render_ledger(town))
