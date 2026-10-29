"""The map of the commons: ``commons/knowledge/INDEX.md``.

Regenerated at every upkeep (and by ``babo reindex``). It lists every note in
the commons by topic, with Obsidian-style ``[[links]]``, so a resident (or
Squid, in Obsidian) can find its way around without searching. Residents
follow a link with ``read_note("[[note name]]")``. Notes that were replaced or
are contested are listed apart, so the map leads to what is current.
"""

from __future__ import annotations

from collections import defaultdict

from babo.baton.notes import Note, first_line
from babo.baton.town import Town, atomic_write

INDEX_NAME = "INDEX.md"
_OTHER = "Other notes"


def _line(note: Note) -> str:
    gist = first_line(note.body, 110)
    gist = "" if gist.lower() == note.title.lower() else f" — {gist}"
    return f"- [[{note.path.stem}|{note.title}]]{gist}"


def render_map(town: Town) -> str:
    notes = [Note.load(p) for p in sorted(town.knowledge.glob("*.md")) if p.name != INDEX_NAME]
    current = [n for n in notes if n.status == "active"]
    topics: dict[str, list[Note]] = defaultdict(list)
    for note in current:
        for keyword in note.keywords[:3] or [_OTHER]:
            topics[keyword.strip().lower() if keyword != _OTHER else _OTHER].append(note)
    lines = [
        "# The town's knowledge",
        "",
        "A map of the commons, by topic. Generated at every upkeep from the notes' keywords; edits here are overwritten.",
        "Follow a link with `read_note(\"[[note name]]\")`, or search with `search_memory`.",
        "",
    ]
    if not current:
        lines.append("Nothing in the commons yet.")
    order = sorted((t for t in topics if t != _OTHER), key=lambda t: (-len(topics[t]), t))
    for topic in [*order, *([_OTHER] if _OTHER in topics else [])]:
        lines += ["", f"## {topic[:1].upper() + topic[1:]}", ""]
        lines += [_line(n) for n in sorted(topics[topic], key=lambda n: n.title.lower())]
    older = [n for n in notes if n.status != "active"]
    if older:
        lines += ["", "## Replaced or contested", ""]
        lines += [f"{_line(n)} ({n.status})" for n in sorted(older, key=lambda n: n.title.lower())]
    return "\n".join(lines) + "\n"


def write_map(town: Town) -> bool:
    town.knowledge.mkdir(parents=True, exist_ok=True)
    return atomic_write(town.knowledge / INDEX_NAME, render_map(town))
