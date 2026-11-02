"""A task list an agent keeps as it works (step 19).

For work with several steps, an agent writes down what it will do and keeps it up
to date (``update_tasks``: the whole list each time, each item ``todo``, ``doing``,
``done`` or ``dropped``). Small models lose track of a plan over a long turn; a list
the harness shows back keeps them on it:

- the tool's result is the list as it now stands;
- the list is shown again with each new message from the person (only while
  something on it is still open), for the Mayor and for residents in opencode;
- the owner sees it: on the Sessions tab, and with ``babo tasks``.

One list per session (a conversation of the Mayor's, or a resident's session). It
is the agent's working list, not its memory: what carries over to the next
session is the baton's *Next*.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from babo.baton.town import BatonError, Town, atomic_write, now_iso, slugify

STATUSES = ("todo", "doing", "done", "dropped")
OPEN = ("todo", "doing")
MAX_ITEMS = 20
MAX_TEXT = 200
_MARK = {"todo": "[ ]", "doing": "[>]", "done": "[x]", "dropped": "[-]"}


@dataclass
class Item:
    task: str
    status: str = "todo"


@dataclass
class TaskList:
    resident: str
    session: str
    items: list[Item] = field(default_factory=lambda: cast(list[Item], []))
    updated: str = ""

    @property
    def open(self) -> list[Item]:
        return [i for i in self.items if i.status in OPEN]

    def counts(self) -> dict[str, int]:
        return {"done": sum(1 for i in self.items if i.status == "done"), "total": len([i for i in self.items if i.status != "dropped"])}

    def text(self) -> str:
        """The list as the agent sees it."""
        if not self.items:
            return "Your task list is empty."
        c = self.counts()
        lines = [f"Your task list ({c['done']} of {c['total']} done):"]
        lines += [f"{n}. {_MARK[i.status]} {i.task}" for n, i in enumerate(self.items, 1)]
        return "\n".join(lines)

    def reminder(self) -> str:
        """What comes with the person's next message, while something on it is still open."""
        if not self.open:
            return ""
        return (f"[Your task list, from Babo: keep it up to date with `update_tasks`.]\n{self.text()}")


def _dir(town: Town, resident: str) -> Path:
    return town.state / "tasks" / slugify(resident, 40)


def path(town: Town, resident: str, session: str) -> Path:
    return _dir(town, resident) / f"{slugify(session.replace(':', '-'), 120) or 'direct'}.json"


def reminder_path(town: Town, resident: str, session: str) -> Path:
    return path(town, resident, session).with_suffix(".txt")


def load(town: Town, resident: str, session: str) -> TaskList:
    try:
        data = cast(dict[str, Any], json.loads(path(town, resident, session).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return TaskList(resident, session)
    items = [Item(str(i.get("task") or ""), str(i.get("status") or "todo")) for i in cast(list[dict[str, Any]], data.get("items") or [])]
    return TaskList(resident, session, [i for i in items if i.task], str(data.get("updated") or ""))


def parse_items(raw: object) -> list[Item]:
    if not isinstance(raw, list):
        raise BatonError("`items` is the whole list: [{task, status}], status todo, doing, done or dropped")
    out: list[Item] = []
    for entry in cast(list[object], raw):
        if isinstance(entry, str):
            entry = {"task": entry}
        if not isinstance(entry, dict):
            raise BatonError("each item is {task, status}")
        item = cast(dict[str, Any], entry)
        task = " ".join(str(item.get("task") or item.get("text") or "").split())
        status = str(item.get("status") or "todo").strip().lower()
        if not task:
            raise BatonError("each item needs its task, in a few words")
        if status not in STATUSES:
            raise BatonError(f"`{status}` isn't a status: todo, doing, done or dropped")
        out.append(Item(task[:MAX_TEXT], status))
    if len(out) > MAX_ITEMS:
        raise BatonError(f"keep the list to {MAX_ITEMS} items: group small steps together")
    return out


def update(town: Town, resident: str, session: str, raw: object) -> str:
    """Replace the session's list with ``raw`` (the whole list). Returns it as the agent sees it."""
    tasks = TaskList(resident, session, parse_items(raw), now_iso())
    target = path(town, resident, session)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, json.dumps({"items": [asdict(i) for i in tasks.items], "updated": tasks.updated}, ensure_ascii=False) + "\n")
    atomic_write(reminder_path(town, resident, session), tasks.reminder())  # what Babo's opencode plugin shows
    said = tasks.text()
    doing = [i for i in tasks.items if i.status == "doing"]
    if len(doing) > 1:
        said += "\n\n(Several are marked doing: finish one before the next, where you can.)"
    if tasks.items and not tasks.open:
        said += "\n\nEverything on it is done."
    return said


def all_lists(town: Town, resident: str | None = None) -> list[TaskList]:
    """Every session's list that has something on it, newest first (for the owner)."""
    out: list[TaskList] = []
    base = town.state / "tasks"
    for folder in sorted(base.iterdir()) if base.exists() else []:
        if resident and folder.name != slugify(resident, 40):
            continue
        for file in sorted(folder.glob("*.json")):
            try:
                data = cast(dict[str, Any], json.loads(file.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
            items = [Item(str(i.get("task") or ""), str(i.get("status") or "todo")) for i in cast(list[dict[str, Any]], data.get("items") or [])]
            if items:
                out.append(TaskList(folder.name, file.stem, items, str(data.get("updated") or "")))
    return sorted(out, key=lambda t: t.updated, reverse=True)


def overview(tasks: TaskList) -> dict[str, Any]:
    return {"resident": tasks.resident, "session": tasks.session, "updated": tasks.updated, **tasks.counts(),
            "open": len(tasks.open), "items": [asdict(i) for i in tasks.items]}
