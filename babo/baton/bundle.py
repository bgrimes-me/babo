"""The session start bundle: what a resident sees at the top of every session.

A pure function of the town directory and a resident id, so it can be built
from outside babo (the OpenCode plugin in phase two) and printed exactly
by ``babo context``.

It comes in two parts. What stays the same for a whole session (who the
resident is, SELF, ROLE, the neighbours, the charter pointers, the LEDGER) goes
at the start of the prompt, where the model server's prompt cache keeps it.
What changes as the session goes (the BATON entry for the current thread,
unread letters and flags, top search hits) goes at the end, where it also
carries the most weight: ``now_text``. Budget pressure trims search hits first,
then letter previews, then the ledger, then the baton entry. SELF and ROLE are
never trimmed. CHARTER and HOUSE are given in full in a resident's first
session and as one-line pointers after that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from babo.baton.baton_file import current_entry
from babo.baton.inbox import unread
from babo.baton.notes import first_line
from babo.baton.registry import Registry
from babo.baton.scope import Scope
from babo.baton.search import Hit, open_index
from babo.baton.tokens import estimate_tokens, trim_to_tokens
from babo.baton.town import Town

# The sections that change during a session (they go last), and how that part starts.
NOW_SECTIONS = ("baton", "letters", "search")
NOW_HEAD = "[Where you are now: your baton, new letters and notes. From Babo, not from the person you're talking with.]"


@dataclass
class Bundle:
    resident_id: str
    first_session: bool
    sections: list[tuple[str, str]] = field(default_factory=list)  # (name, text)
    trimmed: list[str] = field(default_factory=list)

    def text(self) -> str:
        return "\n\n".join(text for _, text in self.sections if text.strip())

    def stable_text(self) -> str:
        """The part that stays the same all session: the start of the prompt."""
        return "\n\n".join(text for name, text in self.sections if name not in NOW_SECTIONS and text.strip())

    def now_text(self) -> str:
        """Where the resident is now: given at the end of the conversation, and again when it changes."""
        body = "\n\n".join(text for name, text in self.sections if name in NOW_SECTIONS and text.strip())
        return f"{NOW_HEAD}\n\n{body}" if body else ""

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text())


def stable_inputs(town: Town, resident_id: str) -> list[Path]:
    """The files the start of the prompt is made of: when one changes, it is built again."""
    home = town.resident_dir(resident_id)
    return [home / "SELF.md", home / "ROLE.md", town.ledger, town.registry_json]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _hits_text(hits: list[Hit]) -> str:
    if not hits:
        return ""
    lines = ["## Possibly relevant notes", "", "Search results for your current thread. Open one with `read_note`."]
    for hit in hits:
        where = "commons" if hit.owner == "commons" else "mine"
        flag = f" [{hit.status}]" if hit.status != "active" else ""
        lines.append(f"- {hit.title}{flag} ({where}: `{hit.path}`) — {hit.snippet.strip()}")
    return "\n".join(lines)


def build_bundle(
    town: Town,
    resident_id: str,
    *,
    max_tokens: int = 2200,
    search_hits: int = 3,
    letter_preview_lines: int = 5,
    index_path: Path | None = None,
    thread: str | None = None,
) -> Bundle:
    resident = Registry(town).require_active(resident_id)
    home = town.resident_dir(resident_id)
    bundle = Bundle(resident_id=resident_id, first_session=not resident.first_session_done)

    self_text = _read(home / "SELF.md")
    role_text = _read(home / "ROLE.md")
    who = f"# You\n\nYou are the resident `{resident_id}` of {town.display_name}"
    who += f", office: {resident.office}." if resident.name == "unchosen" else f", named {resident.name}, office: {resident.office}."
    if resident.name == "unchosen":
        who += " You haven't chosen a name; that is yours to do if and when you want (`revise_self` with `name`)."

    others = [r for r in Registry(town).all() if r.id != resident_id]
    if others:
        neighbours = "## Neighbours\n\n" + "\n".join(
            f"- `{r.id}`" + ("" if r.name == "unchosen" else f", named {r.name}") + f", office: {r.office}"
            + ("" if r.status == "active" else f" ({r.status})") for r in sorted(others, key=lambda r: r.founded + r.id)
        ) + "\n\nReach them with `send_letter` or `visit`. Who lives here is also in `read_note(\"REGISTRY.md\")`."
    else:
        neighbours = "## Neighbours\n\nNo one else lives here yet."

    # Fixed, never trimmed.
    fixed: list[tuple[str, str]] = [("who", who), ("self", self_text), ("role", role_text), ("neighbours", neighbours)]
    if bundle.first_session:
        fixed.append(("charter", _read(town.charter)))
        fixed.append(("house", _read(town.house)))
        fixed.append((
            "welcome",
            "## First session\n\nThis is your first session. The charter, the house agreements and your role are above. "
            "Nothing is expected of you yet; take your time to settle in.",
        ))
    else:
        fixed.append((
            "pointers",
            "Charter: how this town works and what you own — `read_note(\"CHARTER.md\")`.\n"
            "House agreements: what we agreed about the machine — `read_note(\"HOUSE.md\")`.",
        ))

    entry = current_entry(home, thread)
    baton_text = ("# Where you left off\n\n" + entry.render()) if entry else ""
    ledger_text = _read(town.ledger)

    letters = unread(town, resident_id)
    letters_text = ""
    if letters:
        flags = sum(1 for i in letters if i.kind == "flag")
        head = f"## Inbox\n\nYou have {len(letters)} unread item(s)" + (f", {flags} of them flag(s)" if flags else "") + "."
        previews = [f"- {i.kind} from {i.sender}: {first_line(i.body, 90)} (`{Scope(town, resident_id).display(i.path)}`)" for i in letters]
        letters_text = head + "\n" + "\n".join(previews[:letter_preview_lines])
        if len(previews) > letter_preview_lines:
            letters_text += f"\n- … and {len(previews) - letter_preview_lines} more in `inbox/`"

    hits: list[Hit] = []
    if search_hits and index_path is not None and entry is not None:
        query = " ".join([entry.thread, entry.doing, *entry.next])
        try:
            hits = open_index(index_path).search(query, owners=("commons", resident_id), k=search_hits)
        except Exception:
            hits = []
    hits_text = _hits_text(hits)

    budget = max_tokens - sum(estimate_tokens(t) for _, t in fixed)
    variable = [("baton", baton_text), ("ledger", ledger_text), ("letters", letters_text), ("search", hits_text)]

    def used() -> int:
        return sum(estimate_tokens(t) for _, t in variable)

    # Trim order: search hits, letters, ledger, baton.
    for name in ("search", "letters", "ledger", "baton"):
        if used() <= budget:
            break
        idx = next(i for i, (n, _) in enumerate(variable) if n == name)
        text = variable[idx][1]
        if not text:
            continue
        others = used() - estimate_tokens(text)
        allowance = budget - others
        variable[idx] = (name, trim_to_tokens(text, allowance) if allowance > 40 else "")
        bundle.trimmed.append(name)

    bundle.sections = fixed + [s for s in variable if s[0] == "ledger"] + [s for s in variable if s[0] in NOW_SECTIONS]
    return bundle
