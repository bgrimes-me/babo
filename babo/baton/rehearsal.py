"""Rehearsals: a round run so that what it needs is found out before it runs for real.

Each step runs as a session of its resident in opencode, with the access its
round already gives it, its own folder to write in, and everything else
(commands, web pages, crew) set to *ask*:

- **Unattended**: every question is refused, and written down exactly.
- **Live**: the owner watches; each question waits for them on the Sessions
  tab (*Allow once*, *Refuse*), and each answer is written down.

Whatever the resident changes in its own folder is put back afterwards (its
memory of having rehearsed stays: that's its own). The **report** lists, per
step, what it asked for and what came of it, and the access Babo proposes from
that: exact shell commands rather than "the shell", the same rule as errands.
One step of approving writes that access into the round.

A step that needs a refused command's output to go on stops there; the report
says so. Approve that much and rehearse again to see the rest.

A step whose resident runs in Babo itself (the Mayor) isn't rehearsed: Babo
can't pause its tools to ask, and it always runs with its own profile.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from babo.baton import rounds
from babo.baton.bundle import SELF_NAME
from babo.baton.notes import join_frontmatter, split_frontmatter
from babo.baton.registry import Registry
from babo.baton.town import OWNER, BatonError, Town, atomic_write, now_iso

if TYPE_CHECKING:
    from babo.config.schema import Config

REFUSED_NOTE = ("Not in a rehearsal: Babo has written down that you asked for it. Carry on without it as far as you can, "
                "then say what you'd need.")
LIVE_WAIT_S = 2 * 3600  # how long a live rehearsal waits for the owner's answers
TURN_TIMEOUT_S = 1800
# A resident's memory is its own: what it remembers of rehearsing stays; its work is put back.
_MEMORY = ("checkpoints", "sessions", "inbox", "memory", "BATON.md", SELF_NAME, "knowledge")
_ACTIONS = {"shell": "shell", "bash": "shell", "webfetch": "web_fetch", "websearch": "web_search", "subagent": "subagents",
            "task": "subagents"}


@dataclass
class Asked:
    action: str
    resources: list[str]
    decision: str  # refused | allowed | allowed for the session | (unanswered)


@dataclass
class StepReport:
    office: str
    resident: str = ""
    status: str = ""  # rehearsed | skipped | not rehearsed | failed
    note: str = ""
    asked: list[Asked] = field(default_factory=lambda: cast(list[Asked], []))
    report: str = ""
    stopped: bool = False  # something it asked for was refused: it may not have got to the end


@dataclass
class Rehearsal:
    id: str
    round: str
    live: bool
    started: str = field(default_factory=now_iso)
    finished: str = ""
    status: str = "running"  # running | done | failed
    steps: list[StepReport] = field(default_factory=lambda: cast(list[StepReport], []))
    approved: str = ""  # when its proposed access was approved into the round

    def proposal(self) -> list[dict[str, Any]]:
        """Per step: the access Babo proposes from what was asked (and, live, allowed), and what a round can't be given."""
        out: list[dict[str, Any]] = []
        for step in self.steps:
            access: dict[str, str] = {}
            commands: list[str] = []
            cannot: list[str] = []
            for asked in step.asked:
                if self.live and not asked.decision.startswith("allowed"):
                    continue  # live: only what the owner allowed becomes the round's access
                kind = _ACTIONS.get(asked.action, asked.action)
                if kind == "shell":
                    commands += [c for c in asked.resources if c and c != "*" and c not in commands]
                elif kind in ("web_fetch", "web_search", "subagents"):
                    access[kind] = "allow"
                elif kind == "read":
                    access["read"] = "town"
                else:
                    cannot.append(f"{asked.action} {' · '.join(asked.resources)}".strip())
            out.append({"office": step.office, "access": access, "commands": commands, "cannot": cannot})
        return out


def _strings(value: object) -> list[str]:
    return [str(x) for x in cast(list[object], value)] if isinstance(value, list) else []


# -- where reports are kept ----------------------------------------------------------


def _folder(town: Town) -> Path:
    return town.state / "rounds" / "rehearsals"


def _path(town: Town, rehearsal_id: str) -> Path:
    if not rehearsal_id or "/" in rehearsal_id or rehearsal_id.startswith("."):
        raise BatonError(f"'{rehearsal_id}' is not a rehearsal")
    return _folder(town) / f"{rehearsal_id}.json"


def save(town: Town, r: Rehearsal) -> None:
    path = _path(town, r.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(asdict(r), indent=2, ensure_ascii=False) + "\n")


def load(town: Town, rehearsal_id: str) -> Rehearsal:
    try:
        data = cast(dict[str, Any], json.loads(_path(town, rehearsal_id).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise BatonError(f"There's no rehearsal '{rehearsal_id}'.") from exc
    steps: list[StepReport] = []
    for raw in cast(list[dict[str, Any]], data.pop("steps", [])):
        asked = [Asked(**a) for a in cast(list[dict[str, Any]], raw.pop("asked", []))]
        steps.append(StepReport(**raw, asked=asked))
    return Rehearsal(**data, steps=steps)


def latest(town: Town, round_name: str) -> Rehearsal | None:
    found = sorted(_folder(town).glob(f"{round_name}--*.json")) if _folder(town).exists() else []
    return load(town, found[-1].stem) if found else None


# -- the owner's answers in a live rehearsal ---------------------------------------------


def note_answer(town: Town, session: Any, question: dict[str, Any], decision: str) -> None:
    """Called when the owner answers a question in a session that rehearses a step: written into its report."""
    rehearsal_id, _, index = str(session.rehearsal).rpartition(":")
    try:
        r = load(town, rehearsal_id)
        step = r.steps[int(index) - 1]
    except (BatonError, ValueError, IndexError):
        return
    said = {"once": "allowed", "always": "allowed for the session", "reject": "refused"}.get(decision, decision)
    step.asked.append(Asked(str(question.get("action") or ""), _strings(question.get("resources")), said))
    step.stopped = step.stopped or said == "refused"
    save(town, r)


# -- running one -------------------------------------------------------------------------


def _snapshot(home: Path) -> Path:
    """A copy of the resident's work (not its memory), to put back after the rehearsal."""
    snap = Path(tempfile.mkdtemp(prefix="babo-rehearsal-"))
    shutil.copytree(home, snap / "home", ignore=lambda folder, names: [n for n in names if Path(folder) == home and n in _MEMORY],
                    symlinks=True)
    return snap


def _restore(home: Path, snap: Path) -> None:
    saved = snap / "home"
    for item in list(home.iterdir()):
        if item.name in _MEMORY:
            continue
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()
    for item in saved.iterdir():
        target = home / item.name
        if item.is_dir() and not item.is_symlink():
            shutil.copytree(item, target, symlinks=True)
        else:
            shutil.copy2(item, target, follow_symlinks=False)
    shutil.rmtree(snap, ignore_errors=True)


def _rehearsal_access(step: rounds.Step) -> dict[str, str]:
    """What a step may do while it rehearses: what its round already gives it, its own folder, and the rest asks."""
    from babo.baton.profiles import CAPABILITIES

    access = {c.key: ("allow" if step.access.get(c.key) == "allow" else "ask") for c in CAPABILITIES}
    access["files"] = "allow"  # its own folder only (the fence keeps the rest out); put back afterwards
    access["read"] = step.access.get("read", "own")
    return access


def _wait_for_turn(town: Town, config: Config, session: Any, sent: str, report: StepReport, *, live: bool) -> str:
    from babo.baton import opencode

    deadline = time.monotonic() + (LIVE_WAIT_S if live else TURN_TIMEOUT_S)
    seen: set[str] = set()
    while time.monotonic() < deadline:
        run = opencode.running(town, session)
        if run is None:
            raise BatonError("its session stopped")
        state = opencode.live(town, session)
        if state["state"] == "waiting" and not live:
            for q in cast(list[dict[str, Any]], state["questions"]):
                if str(q.get("id")) in seen:
                    continue
                seen.add(str(q.get("id")))
                report.asked.append(Asked(str(q.get("action") or ""), _strings(q.get("resources")), "refused"))
                report.stopped = True
                opencode.answer_permission(town, session, str(q["id"]), "reject", message=REFUSED_NOTE)
        after = opencode._after(opencode._messages(run, session), sent)  # pyright: ignore[reportPrivateUsage]
        if any(m.get("type") == "idle" for m in after):
            return "\n".join(opencode._text(m) for m in after if m.get("type") == "assistant" and opencode._text(m)).strip()  # pyright: ignore[reportPrivateUsage]
        time.sleep(1)
    raise BatonError("it didn't finish its step in time" + (" (nobody answered its questions)" if live else ""))


def rehearse(town: Town, config: Config, round_name: str, *, live: bool, config_path: Path) -> Rehearsal:
    """Rehearse every step of a round, one after the other, and keep the report."""
    from babo.baton import notices, opencode

    r = rounds.get(town, round_name)
    stamp = now_iso()[:19].replace(":", "").replace("T", "-")
    rehearsal = Rehearsal(id=f"{round_name}--{stamp}", round=round_name, live=live)
    save(town, rehearsal)
    previous = ""
    for index, step in enumerate(r.steps, 1):
        report = StepReport(step.office)
        rehearsal.steps.append(report)
        resident = step.who(town)
        if not resident:
            report.status, report.note = "skipped", "vacancy: nobody holds this office"
            continue
        report.resident = resident
        agent = Registry(town).require_active(resident)
        if not opencode.runs_in_opencode(agent.office) or opencode.opencode_bin(config) is None:
            report.status, report.note = "not rehearsed", "runs in Babo itself, with its own profile"
            continue
        if step.material == "promotions":
            material, _ = rounds.promotions_material(town)  # (nothing is marked reviewed: it's a rehearsal)
        elif step.material == "inspection":
            material = rounds.inspection_material(town, town.state / "rounds" / "no-pass")
        elif step.material == "previous":
            material = f"What the step before reported:\n\n{previous}" if previous else ""
        else:
            material = "-"
        message = (f"{rounds.MARK} A rehearsal of {r.title}, step {index} of {len(r.steps)}. Try your duty as you would for real; "
                   "what you change in your folder will be put back, and Babo notes what you ask to do, so the round can be given "
                   f"exactly that. Your duty: {step.duty}" + ("" if material in ("", "-") else f"\n\n{material}")
                   + "\n\nWhen you're done (or can't go on), say in a few lines what you did and what you'd need.")
        session = opencode.new_session(town, config, resident, title=f"Rehearsal: {r.title}, step {index}",
                                       overrides=cast(dict[str, object], _rehearsal_access(step)), commands=list(step.commands),
                                       by=OWNER, kind="work" if live else f"rehearsal:{round_name}")
        session.rehearsal = f"{rehearsal.id}:{index}"
        opencode.save_session(town, session)
        save(town, rehearsal)
        home = town.resident_dir(resident)
        snap = _snapshot(home)
        try:
            sent = opencode.prompt(town, config, session, message, config_path=config_path)
            if live:
                notices.add(town, "rehearsal", f"A live rehearsal of “{r.title}” is waiting for you: step {index} ({resident}) "
                            "will ask for what it needs on the Sessions tab.", resident=resident, session=session.id)
            report.report = _wait_for_turn(town, config, opencode.get_session(town, resident, session.id), sent, report, live=live)
            report.status = "rehearsed"
            previous = report.report
        except (BatonError, OSError) as exc:
            report.status, report.note = "failed", str(exc)
        finally:
            fresh = load(town, rehearsal.id)  # a live rehearsal's answers were written in meanwhile
            if live:
                report.asked = fresh.steps[index - 1].asked
                report.stopped = fresh.steps[index - 1].stopped
            opencode.freeze(town, opencode.get_session(town, resident, session.id))
            done = opencode.get_session(town, resident, session.id)
            done.archived = True
            opencode.save_session(town, done)
            _restore(home, snap)
            save(town, rehearsal)
    rehearsal.finished = now_iso()
    rehearsal.status = "failed" if any(s.status == "failed" for s in rehearsal.steps) else "done"
    save(town, rehearsal)
    notices.add(town, "rehearsal", f"The rehearsal of “{r.title}” is done: its report, and the access it proposes, are on the Rounds tab.")
    return rehearsal


def approve(town: Town, rehearsal_id: str) -> str:
    """Write a rehearsal's proposed access into its round: each step gets exactly what is shown."""
    rehearsal = load(town, rehearsal_id)
    proposal = rehearsal.proposal()
    path = rounds.folder(town) / f"{rehearsal.round}.md"
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    steps = cast(list[dict[str, Any]], meta.get("steps") or [])
    if len(steps) != len(proposal):
        raise BatonError("The round has changed since this rehearsal; rehearse it again.")
    said: list[str] = []
    for i, (step, wanted) in enumerate(zip(steps, proposal, strict=True), 1):
        access = {**cast(dict[str, str], step.get("access") or {}), **wanted["access"]}
        commands = list(dict.fromkeys([*cast(list[str], step.get("commands") or []), *wanted["commands"]]))
        if access:
            step["access"] = access
        if commands:
            step["commands"] = commands
        if wanted["access"] or wanted["commands"]:
            said.append(f"step {i} ({step.get('office')}): " + ", ".join([*(f"{k} {v}" for k, v in wanted["access"].items()),
                                                                          *(f"may run `{c}`" for c in wanted["commands"])]))
    atomic_write(path, join_frontmatter(meta, body))
    rounds.get(town, rehearsal.round)  # it still reads
    rehearsal.approved = now_iso()
    save(town, rehearsal)
    return f"“{rounds.get(town, rehearsal.round).title}” now has: " + ("; ".join(said) if said else "nothing more than it had") + "."


def set_step_access(town: Town, round_name: str, index: int, access: dict[str, str], commands: list[str] | None = None) -> str:
    """Set a step's access by hand (the Rounds tab, `babo rounds access`)."""
    path = rounds.folder(town) / f"{round_name}.md"
    rounds.get(town, round_name)
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    steps = cast(list[dict[str, Any]], meta.get("steps") or [])
    if not 1 <= index <= len(steps):
        raise BatonError(f"“{round_name}” has {len(steps)} step(s).")
    step = steps[index - 1]
    current = {**cast(dict[str, str], step.get("access") or {}), **access}
    current = {k: v for k, v in current.items() if v not in ("", "profile")}
    if current:
        step["access"] = current
    else:
        step.pop("access", None)
    if commands is not None:
        if commands:
            step["commands"] = commands
        else:
            step.pop("commands", None)
    text = join_frontmatter(meta, body)
    rounds.parse(round_name, text)  # refuse what wouldn't read, before writing it
    atomic_write(path, text)
    return f"Step {index} of “{rounds.get(town, round_name).title}” now: " + (
        ", ".join(f"{k} {v}" for k, v in current.items()) or "as in the profile") + (
        f"; may run {', '.join(f'`{c}`' for c in commands)}" if commands else "") + "."


# -- asked for from the web interface or the terminal: run by the upkeep service ------------


def _requests(town: Town) -> Path:
    return town.state / "runtime" / "rehearsal-requests.json"


def request(town: Town, round_name: str, *, live: bool) -> str:
    r = rounds.get(town, round_name)
    path = _requests(town)
    try:
        wanted = cast(list[dict[str, Any]], json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        wanted = []
    wanted.append({"round": round_name, "live": live, "at": now_iso()})
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(wanted) + "\n")
    how = "live: its questions will come to you on the Sessions tab" if live else "unattended: what it asks for is written down"
    return f"A rehearsal of “{r.title}” starts within a minute ({how})."


def next_request(town: Town) -> dict[str, Any] | None:
    """Take the oldest rehearsal asked for (one at a time)."""
    path = _requests(town)
    try:
        wanted = cast(list[dict[str, Any]], json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    if not wanted:
        return None
    first, rest = wanted[0], wanted[1:]
    atomic_write(path, json.dumps(rest) + "\n")
    return first
