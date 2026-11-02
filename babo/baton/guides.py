"""Guides the harness brings in at the right moment.

Research on small models is clear: they rarely open a document they are only
told is there. So the harness decides when a guide is needed, and brings it in:

- **An index, always there.** One line per guide in the start of the prompt:
  its name and *when* to read it. Small and fixed, so the prompt cache holds.
- **Brought in when it's needed**: with the result of the first call of a tool
  in a session (the letters guide with the first ``send_letter``), or of a call
  whose result matches an error (the "when something is refused" guide after a
  refusal). Once per session each. For the Mayor, Babo adds it to the tool's
  result; for a resident in opencode, Babo's plugin does.
- **A role's guides** are listed only for that office.
- **Opened by hand**: any guide can be read with ``read_note("guides/<name>.md")``.
  Babo logs it; a guide opened by hand often is a sign it needs a trigger
  (``babo guides`` says so).

Guides live in the commons (``commons/guides/``), as notes with a little
frontmatter: ``title``, ``when``, and the triggers (``tools``, ``errors``,
``offices``). Babo ships a few, kept up to date while nobody has edited them;
the owner (and the Archivist, through review) may change them or add more.

**Skills** (step 18) are brought in the same way: the town's skills
(``commons/skills/<name>/SKILL.md``, written by the Archivist) are listed in the
ledger (which, unlike this index, is trimmed before a resident's baton), and
brought in when they're needed: with the
first call of a tool they name, or with the turn's message when it has one of
their ``keywords`` in it. Their frontmatter is a skill's usual one (``name``,
``description``), plus Babo's triggers: ``when``, ``keywords``, ``tools``,
``offices``. A skill without triggers is still in the index, to be read by hand
(``read_note("skill:<name>")``).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from babo.baton.notes import split_frontmatter
from babo.baton.town import Town

GUIDES_DIR = "guides"
EVENT = "guide"
SKILL_MAX_CHARS = 6000  # a skill brought in whole up to this; past it, the start and where to read the rest
# Tool names as opencode or MCP clients show them, to Babo's own.
_PREFIXES = ("babo_",)
_ALIASES = {"bash": "shell", "exec": "shell"}


@dataclass(frozen=True)
class Guide:
    name: str
    title: str
    when: str
    body: str
    tools: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    offices: tuple[str, ...] = ()  # empty: every resident
    keywords: tuple[str, ...] = ()  # words in the turn's message that bring it in
    kind: str = "guide"  # guide | skill

    def for_office(self, office: str) -> bool:
        return not self.offices or office in self.offices

    def text(self) -> str:
        """The guide as it is brought in: marked as Babo's, so it isn't mistaken for the tool's output."""
        if self.kind == "skill":
            body = self.body.strip()
            if len(body) > SKILL_MAX_CHARS:
                body = body[:SKILL_MAX_CHARS].rstrip() + f"\n\n… (the rest: read_note(\"skill:{self.name}\"))"
            return f"[Skill from Babo: {self.name}. Follow it for this.]\n\n{body}\n\n[End of the skill]"
        return f"[Guide from Babo: {self.title}. Read it before you go on.]\n\n{self.body.strip()}\n\n[End of the guide]"

    def matches(self, message: str) -> bool:
        """Whether the message has one of its keywords (whole words, any case)."""
        return any(re.search(rf"(?<![\w-]){re.escape(k)}(?![\w-])", message, re.IGNORECASE) for k in self.keywords if k.strip())


def folder(town: Town) -> Path:
    return town.commons / GUIDES_DIR


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(v) for v in cast(list[object], value) if str(v).strip())
    return ()


def load(town: Town) -> list[Guide]:
    """The town's guides, then its skills."""
    return load_guides(town) + load_skills(town)


def load_skills(town: Town) -> list[Guide]:
    out: list[Guide] = []
    for path in sorted(town.skills.glob("*/SKILL.md")):
        try:
            meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not body.strip():
            continue
        name = str(meta.get("name") or path.parent.name)
        out.append(Guide(name, name, str(meta.get("when") or meta.get("description") or ""), body,
                         tuple(_tool(t) for t in _strings(meta.get("tools"))), (), _strings(meta.get("offices")),
                         _strings(meta.get("keywords")), "skill"))
    return out


def load_guides(town: Town) -> list[Guide]:
    out: list[Guide] = []
    for path in sorted(folder(town).glob("*.md")):
        try:
            meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not body.strip():
            continue
        errors = tuple(e for e in _strings(meta.get("errors")) if _valid(e))
        out.append(Guide(path.stem, str(meta.get("title") or path.stem), str(meta.get("when") or ""), body,
                         tuple(_tool(t) for t in _strings(meta.get("tools"))), errors, _strings(meta.get("offices"))))
    return out


def _valid(pattern: str) -> bool:
    try:
        re.compile(pattern)
    except re.error:
        return False
    return True


def _tool(name: str) -> str:
    from babo.baton.aliases import TOOLS

    for prefix in _PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
    name = TOOLS.get(name, name)  # an old tool name (step 28)
    return _ALIASES.get(name, name)


def index_text(town: Town, office: str) -> str:
    """The always-present index: one line per guide, saying when to read it."""
    # (Skills aren't listed here: the ledger lists them, and it's trimmed before the baton is; this index
    # never is, so it stays the guides' few lines.)
    mine = [g for g in load_guides(town) if g.for_office(office)]
    if not mine:
        return ""
    lines = ["## Guides", "", "Babo brings a guide in when you need it, and a skill when it fits. Read a guide any time with "
             "`read_note(\"guides/<name>.md\")`, a skill with `read_note(\"skill:<name>\")`."]
    lines += [f"- `{g.name}`: {g.when or g.title}" for g in mine]
    return "\n".join(lines)


@dataclass
class Shown:
    """Which guides a session has been given, so each comes once."""

    names: dict[str, set[str]] = field(default_factory=lambda: cast(dict[str, set[str]], {}))

    def due(self, session: str, guides: list[Guide], tool: str, result: str, *, failed: bool = True) -> list[tuple[Guide, str]]:
        """The guides this call brings in: for its tool, or (when it failed) for what its error says."""
        seen = self.names.setdefault(session, set())
        name = _tool(tool)
        out: list[tuple[Guide, str]] = []
        for guide in guides:
            if guide.name in seen:
                continue
            if name in guide.tools:
                out.append((guide, f"tool:{name}"))
            elif failed and any(re.search(pattern, result) for pattern in guide.errors):
                out.append((guide, "error"))
        seen.update(g.name for g, _ in out)
        return out

    def due_for_message(self, session: str, guides: list[Guide], message: str) -> list[Guide]:
        """The skills (and guides) the turn's message calls for, by their keywords. Once per session each."""
        seen = self.names.setdefault(session, set())
        out = [g for g in guides if g.keywords and g.name not in seen and g.matches(message)]
        seen.update(g.name for g in out)
        return out


def plugin_file(town: Town, office: str) -> str:
    """What Babo's opencode plugin needs to bring guides in itself: the triggers and the text."""
    return json.dumps([{**{k: v for k, v in asdict(g).items() if k in ("name", "tools", "errors", "keywords", "kind")}, "text": g.text()}
                       for g in load(town) if g.for_office(office) and (g.tools or g.errors or g.keywords)], ensure_ascii=False)


def is_guide_path(town: Town, path: Path) -> str:
    """The guide's (or skill's) name, if ``path`` is one (for logging guides opened by hand)."""
    try:
        resolved = path.resolve()
        if resolved.parent == folder(town).resolve() and resolved.suffix == ".md":
            return resolved.stem
        if resolved.name == "SKILL.md" and resolved.parent.parent == town.skills.resolve():
            skill = next((g for g in load_skills(town) if town.skills.resolve() / resolved.parent.name / "SKILL.md" == resolved), None)
            return skill.name if skill else resolved.parent.name
    except OSError:
        pass
    return ""


def stats(town: Town) -> list[dict[str, Any]]:
    """Each guide with how often it was brought in, and opened by hand, across the town."""
    from babo.baton.events import resident_events
    from babo.baton.registry import Registry

    brought: dict[str, int] = {}
    opened: dict[str, int] = {}
    for resident in Registry(town).all():
        for event in resident_events(town, resident.id):
            if event.get("kind") != EVENT:
                continue
            name = str(event.get("name") or "")
            target = opened if event.get("how") == "opened" else brought
            target[name] = target.get(name, 0) + 1
    rows: list[dict[str, Any]] = []
    for g in load(town):
        hint = ""
        if opened.get(g.name, 0) >= 3 and opened.get(g.name, 0) > brought.get(g.name, 0):
            hint = "Opened by hand more often than brought in: it may need a trigger (tools, errors or keywords)."
        if g.kind == "skill" and not hint and not (g.tools or g.keywords):
            hint = "No triggers: it's only read by hand. Give it keywords (or tools) to be brought in when it fits."
        rows.append({"name": g.name, "title": g.title, "when": g.when, "tools": list(g.tools), "errors": list(g.errors),
                     "offices": list(g.offices), "keywords": list(g.keywords), "kind": g.kind,
                     "brought": brought.get(g.name, 0), "opened": opened.get(g.name, 0), "hint": hint})
    return rows


# -- the Mayor (and any resident running in Babo itself) -------------------------------

_MARK = "_baton_guides"


def install(tool: Any, *, town: Town, resident_id: str, office: str, shown: Shown, watch: Any = None) -> None:
    """Add what the harness has to say to a tool's results: a guide that's needed now, and (with a
    ``drift.Watch``) a reminder when the agent goes round in circles. The tool's ``execute`` is
    wrapped in place, like the commit gate."""
    if getattr(tool, _MARK, False):
        return
    from babo.agent.tools.base import ToolResult
    from babo.agent.tools.context import current_request_session_key
    from babo.baton import drift
    from babo.baton.events import append_event

    inner = tool.execute
    name = str(tool.name)

    def extras(result: str, failed: bool, args: dict[str, Any]) -> list[str]:
        key = current_request_session_key()
        session = key or "direct"
        out: list[str] = []
        for guide, how in shown.due(session, [g for g in load(town) if g.for_office(office)], name, result, failed=failed):
            append_event(town, resident_id, key, EVENT, name=guide.name, how=how)
            out.append(guide.text())
        tripped = watch.see(session, name, args, failed=failed, result=result) if watch is not None else None
        if tripped is not None:
            rule, times = tripped
            append_event(town, resident_id, key, drift.EVENT, rule=rule, tool=name, times=times)
            out.append(drift.reminder(rule, name))
            if times == 2:
                drift.tell_owner(town, resident_id, session, "", rule, name)
        return out

    async def execute(**kwargs: Any) -> Any:
        try:
            result = await inner(**kwargs)
        except Exception as exc:
            said = extras(str(exc), True, kwargs)
            if not said:
                raise
            return ToolResult.error(f"Error: {exc}\n\n" + "\n\n".join(said))
        if not isinstance(result, str):
            return result
        failed = bool(getattr(result, "is_error", False)) or result.startswith(("Error", "Blocked"))
        said = extras(result, failed, kwargs)
        if not said:
            return result
        return ToolResult(result + "\n\n" + "\n\n".join(said), is_error=bool(getattr(result, "is_error", False)))

    object.__setattr__(tool, "execute", execute)
    object.__setattr__(tool, _MARK, True)
