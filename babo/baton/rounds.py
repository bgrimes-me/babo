"""Rounds: the town's work.

Babo's own work (memory forming, the commons as a store, the records, the clock)
is upkeep, and always runs. Everything an office does is in a **round**: what a
watchman, a postman or a doctor does, at set times or when something happens,
going from house to house. Each is a readable file in the town
(``rounds/<name>.md``): a title, when it runs, and its steps in order.

    ---
    title: The night round
    when: model-free            # or {daily: "03:00"}, {weekly: "monday 08:00"}, {every_hours: 6},
                                #    {event: checkpoint | session-wrapped-up | proposal}, {after: <round>}
    runs: wait                  # wait (for a free model; the default) or now (when triggered, at the lowest priority)
    steps:
      - office: archivist
        or: mayor               # who does it while the office is vacant (optional)
        duty: Curate the notes waiting in the queue into the commons.
        material: promotions    # what Babo gathers for it: promotions | inspection | previous | none
      - office: inspector
        duty: Review pending tools, open proposals and today's changes.
        material: inspection
    ---
    What the round is for, in a few words (for people and for the Mayor).

A step names an **office**, so rounds outlive residents; a step for an office
nobody holds (a vacancy) is skipped, or done by the office named in its ``or``. A step whose material is empty is skipped
too (nothing to curate, nothing to inspect). Each step is that resident's own
turn, with the previous step's report as part of its material when it asks for
``previous``. A step that fails stops the round, and the owner hears about it.

Babo decides when a round runs, gathers its material, files what it did and
keeps the record (``state/rounds/``); the residents do the work.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from babo.baton.notes import first_line, join_frontmatter, split_frontmatter
from babo.baton.registry import Registry
from babo.baton.town import BatonError, Town, atomic_write, now_iso

ROUNDS_DIR = "rounds"
MARK = "[From Babo: a round]"
EVENTS = {"checkpoint": "a checkpoint is filed", "session-wrapped-up": "a session wraps up", "proposal": "a proposal is filed"}
MATERIALS = ("none", "previous", "promotions", "inspection")
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@dataclass(frozen=True)
class Step:
    office: str
    duty: str
    material: str = "none"
    fallback: str = ""  # an office that does it while ``office`` is vacant ("or: mayor")
    # What it may do beyond (or short of) the office's profile, as for a session (shell: allow, read: town),
    # and exact shell commands it may run. Approving the round approves exactly these.
    access: dict[str, str] = field(default_factory=lambda: cast(dict[str, str], {}))
    commands: tuple[str, ...] = ()

    def who(self, town: Town) -> str:
        return holder(town, self.office) or (holder(town, self.fallback) if self.fallback else "")


@dataclass(frozen=True)
class Round:
    name: str
    title: str
    when: dict[str, Any]  # {"model_free": True} | {"daily": "03:00"} | {"weekly": "monday 08:00"} | {"every_hours": 6} | {"event": ...} | {"after": ...}
    steps: tuple[Step, ...]
    about: str = ""
    runs: str = "wait"  # wait | now
    enabled: bool = True

    def describe_when(self) -> str:
        w = self.when
        if w.get("model_free"):
            return "whenever there's work for it, in each upkeep pass"
        if "daily" in w:
            return f"every day at {w['daily']}"
        if "weekly" in w:
            day, _, at = str(w["weekly"]).partition(" ")
            return f"every {day.capitalize()} at {at or '00:00'}"
        if "every_hours" in w:
            return f"every {w['every_hours']} hours"
        if "event" in w:
            return f"when {EVENTS.get(str(w['event']), str(w['event']))}"
        if "after" in w:
            return f"after the round “{w['after']}”"
        return "when run by hand"


class RoundError(BatonError):
    """A round file Babo can't use, said plainly."""


def folder(town: Town) -> Path:
    return town.root / ROUNDS_DIR


def _when(raw: object) -> dict[str, Any]:
    if raw in (None, "", "model-free", "model_free"):
        return {"model_free": True}
    if raw in ("manual", "by-hand", "by hand"):
        return {}
    if not isinstance(raw, dict):
        raise RoundError(f"`when` isn't something Babo can check: {raw!r}")
    when = cast(dict[str, Any], raw)
    if "daily" in when:
        if not re.fullmatch(r"\d{1,2}:\d{2}", str(when["daily"])):
            raise RoundError("`daily` takes a time like 03:00")
    elif "weekly" in when:
        day, _, at = str(when["weekly"]).lower().partition(" ")
        if day not in DAYS or (at and not re.fullmatch(r"\d{1,2}:\d{2}", at)):
            raise RoundError("`weekly` takes a day and a time, like monday 08:00")
        when = {"weekly": f"{day} {at or '00:00'}"}
    elif "every_hours" in when:
        try:
            hours = float(when["every_hours"])
        except (TypeError, ValueError) as exc:
            raise RoundError("`every_hours` takes a number") from exc
        if hours < 1:
            raise RoundError("`every_hours` is at least 1")
        when = {"every_hours": hours if hours % 1 else int(hours)}
    elif "event" in when:
        if when["event"] not in EVENTS:
            raise RoundError(f"`event` is one of: {', '.join(EVENTS)}")
    elif "after" in when:
        when = {"after": str(when["after"])}
    else:
        raise RoundError(f"`when` isn't something Babo can check: {raw!r}")
    return when


ACCESS_LEVELS = ("allow", "ask", "deny")


def _access(raw: object) -> dict[str, str]:
    from babo.baton.profiles import CAPABILITIES

    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise RoundError("a step's `access` is settings like shell: allow")
    keys = {c.key for c in CAPABILITIES}
    out: dict[str, str] = {}
    for key, value in cast(dict[str, object], raw).items():
        value = str(value).strip().lower()
        if key == "read":
            if value not in ("own", "town"):
                raise RoundError("a step's `read` is own or town")
        elif key not in keys:
            raise RoundError(f"`{key}` isn't something a step can be given (it can be: {', '.join(sorted(keys))}, read)")
        elif value not in ACCESS_LEVELS:
            raise RoundError(f"a step's `{key}` is allow, ask or deny")
        out[str(key)] = value
    return out


def _commands(raw: object) -> tuple[str, ...]:
    if raw in (None, ""):
        return ()
    items = [raw] if isinstance(raw, str) else cast(list[object], raw) if isinstance(raw, list) else None
    if items is None or any("\n" in str(c) for c in items):
        raise RoundError("a step's `commands` are exact shell commands, one per item")
    return tuple(str(c).strip() for c in items if str(c).strip())


def parse(name: str, text: str) -> Round:
    meta, body = split_frontmatter(text)
    steps: list[Step] = []
    for raw in cast(list[object], meta.get("steps") or []):
        if not isinstance(raw, dict):
            raise RoundError("each step has an office and a duty")
        step = cast(dict[str, Any], raw)
        office, duty, material = str(step.get("office") or "").strip(), str(step.get("duty") or "").strip(), str(step.get("material") or "none")
        if not office or not duty:
            raise RoundError("each step has an office and a duty")
        if material not in MATERIALS:
            raise RoundError(f"`material` is one of: {', '.join(MATERIALS)}")
        steps.append(Step(office, duty, material, str(step.get("or") or "").strip(),
                          _access(step.get("access")), _commands(step.get("commands"))))
    if not steps:
        raise RoundError("a round has at least one step")
    runs = str(meta.get("runs") or "wait")
    if runs not in ("wait", "now"):
        raise RoundError("`runs` is wait (for a free model) or now (when triggered)")
    return Round(name, str(meta.get("title") or name), _when(meta.get("when")), tuple(steps), body.strip(), runs,
                 meta.get("enabled") is not False)


def load(town: Town) -> tuple[list[Round], dict[str, str]]:
    """Every round, and the files that can't be read (name -> why), so a mistake is shown, not hidden."""
    rounds: list[Round] = []
    broken: dict[str, str] = {}
    if not folder(town).exists():  # a town from before rounds: it gets the baseline town's
        from babo.baton.founding import ship_rounds

        ship_rounds(town)
    for path in sorted(folder(town).glob("*.md")):
        try:
            rounds.append(parse(path.stem, path.read_text(encoding="utf-8")))
        except (OSError, RoundError) as exc:
            broken[path.stem] = str(exc)
    return rounds, broken


def get(town: Town, name: str) -> Round:
    for r in load(town)[0]:
        if r.name == name:
            return r
    raise RoundError(f"There's no round called '{name}'. See `babo rounds list`.")


def set_options(town: Town, name: str, *, enabled: bool | None = None, runs: str | None = None) -> Round:
    """Turn a round on or off, or change when it runs; written into its file, so the change is in the town's history."""
    path = folder(town) / f"{name}.md"
    get(town, name)  # it exists and reads
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    if enabled is not None:
        meta["enabled"] = enabled
    if runs is not None:
        if runs not in ("wait", "now"):
            raise RoundError("`runs` is wait (for a free model) or now (when triggered)")
        meta["runs"] = runs
    atomic_write(path, join_frontmatter(meta, body))
    return get(town, name)


# -- holders of an office ----------------------------------------------------------


def holder(town: Town, office: str) -> str:
    """Who holds an office now ("" for a vacancy). The first one founded, if several do."""
    holders = sorted((r for r in Registry(town).all() if r.office == office and r.status == "active"), key=lambda r: r.founded + r.id)
    return holders[0].id if holders else ""


# -- when rounds are due -----------------------------------------------------------


def _state_path(town: Town) -> Path:
    return town.state / "rounds" / "state.json"


def load_state(town: Town) -> dict[str, dict[str, Any]]:
    try:
        data: object = json.loads(_state_path(town).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cast(dict[str, dict[str, Any]], data) if isinstance(data, dict) else {}


def _save_state(town: Town, state: dict[str, dict[str, Any]]) -> None:
    path = _state_path(town)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(state, indent=2, sort_keys=True) + "\n")


def _parse(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.astimezone()


def next_scheduled(r: Round, after: datetime) -> datetime | None:
    """The first scheduled time strictly after ``after`` (None for rounds without a schedule)."""
    w = r.when
    if "every_hours" in w:
        return after + timedelta(hours=float(w["every_hours"]))
    if "daily" in w or "weekly" in w:
        hhmm = str(w.get("daily") or str(w["weekly"]).split(" ", 1)[1])
        hour, minute = (int(x) for x in hhmm.split(":"))
        candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if "weekly" in w:
            target = DAYS.index(str(w["weekly"]).split(" ", 1)[0])
            candidate += timedelta(days=(target - candidate.weekday()) % 7)
            while candidate <= after:
                candidate += timedelta(days=7)
        while candidate <= after:
            candidate += timedelta(days=1)
        return candidate
    return None


def _happened(town: Town, event: str, since: datetime) -> bool:
    """Whether a town event happened after ``since`` (Babo checks, from its own records)."""
    from babo.baton import notices
    from babo.baton.events import resident_events
    from babo.baton.town import today

    if event == "checkpoint":
        days = {today(), (since.date()).isoformat()}
        for resident in Registry(town).all():
            for day in days:
                for e in resident_events(town, resident.id, day=day):
                    if e.get("kind") == "checkpoint" and (_parse(e.get("at")) or since) > since:
                        return True
        return False
    if event == "session-wrapped-up":
        return any(n.get("kind") == "wrapped_up" and (_parse(n.get("at")) or since) > since for n in notices.recent(town))
    if event == "proposal":
        return any(datetime.fromtimestamp(p.stat().st_mtime).astimezone() > since for p in (town.proposals / "open").glob("*.md"))
    return False


def waiting(town: Town, step: Step) -> str:
    """What a step has to work on before a pass starts, in a few words ("" for nothing; the pass may find more)."""
    if step.material == "promotions":
        count = len(pending_promotions(town))
        return f"{count} note(s) to curate into the commons" if count else ""
    if step.material == "inspection":
        count = len(_inspection_items(town))
        return f"{count} tool(s) or proposal(s) to inspect" if count else ""
    return ""


def due(town: Town, *, now: datetime) -> list[tuple[Round, str]]:
    """The rounds to run in the next pass, each with why. Rounds that run whenever there's work
    come along in every pass, and start one themselves when a step has work waiting."""
    state = load_state(town)
    rounds, _ = load(town)
    out: list[tuple[Round, str]] = []
    for r in rounds:
        if not r.enabled:
            continue
        mine = state.get(r.name, {})
        last = _parse(mine.get("last_run")) or _parse(mine.get("created"))
        if last is None:  # first seen: start counting from now, rather than run everything at once
            state.setdefault(r.name, {})["created"] = now.isoformat(timespec="seconds")
            last = now
        w = r.when
        why = ""
        if w.get("model_free"):
            work = [w for w in (waiting(town, s) for s in r.steps if s.who(town)) if w]
            why = f"{r.title}: {'; '.join(work)}" if work else ""
        elif "event" in w:
            why = f"{r.title}: {EVENTS[str(w['event'])]}" if _happened(town, str(w["event"]), last) else ""
        elif "after" in w:
            before = _parse(state.get(str(w["after"]), {}).get("last_run"))
            why = f"{r.title}: after “{w['after']}”" if before is not None and before > last else ""
        else:
            at = next_scheduled(r, last)
            why = f"{r.title}: scheduled for {at:%a %H:%M}" if at is not None and at <= now else ""
        if why:
            out.append((r, why))
    _save_state(town, state)
    return out


# -- material Babo gathers for a step --------------------------------------------------


def pending_promotions(town: Town) -> list[Path]:
    return [p for p in sorted(town.promotions.glob("*.md"))
            if split_frontmatter(p.read_text(encoding="utf-8"))[0].get("status", "pending") == "pending"]


def _inspection_items(town: Town) -> list[Path]:
    from babo.baton.tools.core import proposal_files

    tools = [p for p in sorted((town.tools / "pending").glob("*")) if p.is_dir() and not (p / "REVIEW.md").exists()]
    proposals = [p for p in proposal_files(town.proposals / "open") if not p.with_name(p.stem + ".review.md").exists()]
    return [*tools, *proposals]


def promotions_material(town: Town) -> tuple[str, Callable[[], None]]:
    """The promotion queue, and what to do once the step has been through it (mark it reviewed)."""
    from babo.baton.knowledge_map import INDEX_NAME
    from babo.baton.notes import Note

    pending = pending_promotions(town)
    if not pending:
        return "", lambda: None
    known = [Note.load(p) for p in sorted(town.knowledge.glob("*.md")) if p.name != INDEX_NAME]
    topics = sorted({k.lower() for n in known if n.status == "active" for k in n.keywords})
    lines = [f"The promotion queue has {len(pending)} nomination(s). For each: write it into the commons with "
             "commons_write, or merge it into the note that already covers it (one topic per note). Give it one to three keywords, "
             "reusing the topics below where they fit: they make the map of the commons (commons/knowledge/INDEX.md). Link related "
             "notes in the body with [[note name]]. Supersede notes it makes outdated, and where two views genuinely disagree use "
             "commons_contest to keep both. Write to the author with send_letter when you change or decline their note. "
             "You may decline a nomination; say why in a letter.",
             f"\nTopics so far: {', '.join(topics) or '(none yet)'}",
             "Notes in the commons: " + ("; ".join(f"{n.title} ([[{n.path.stem}]])" for n in known if n.status == "active")[:3000] or "(none yet)")]
    for p in pending:
        meta, body = split_frontmatter(p.read_text(encoding="utf-8"))
        lines.append(f"\n## {meta.get('title')} (from {meta.get('from')}, `{p.relative_to(town.root)}`)\n"
                     f"Reason: {meta.get('reason', '')}\nAliases: {meta.get('aliases', [])} Keywords: {meta.get('keywords', [])}\n\n{body.strip()[:3000]}")

    def reviewed() -> None:
        for p in pending:
            meta, body = split_frontmatter(p.read_text(encoding="utf-8"))
            meta.update({"status": "reviewed", "reviewed": now_iso()})
            atomic_write(p, join_frontmatter(meta, body))

    return "\n".join(lines), reviewed


def inspection_material(town: Town, work_dir: Path) -> str:
    """Today's changes (the pass's diffs), pending tools and open proposals, for review."""
    from babo.baton.tools.core import proposal_files

    parts: list[str] = []
    diffs_dir = work_dir / "diffs"
    for diff in sorted(diffs_dir.glob("*.md")) if diffs_dir.exists() else []:
        parts.append(diff.read_text(encoding="utf-8")[-4000:])
    for tool in sorted(p for p in (town.tools / "pending").glob("*") if p.is_dir()):
        if (tool / "REVIEW.md").exists():
            continue

        def read(name: str, tool: Path = tool) -> str:
            path = tool / name
            return path.read_text(encoding="utf-8")[:2500] if path.exists() else "(missing)"

        parts.append(f"# Pending tool `{tool.name}` (commons/tools/pending/{tool.name})\n\n{read('TOOL.md')}\n\n"
                     f"tool.py:\n```python\n{read('tool.py')}\n```\n\ntest_tool.py:\n```python\n{read('test_tool.py')}\n```\n\n"
                     f"Test result:\n{read('TEST-RESULT.md')}")
    for prop in proposal_files(town.proposals / "open"):
        if prop.with_name(prop.stem + ".review.md").exists():
            continue
        parts.append(f"# Open proposal ({prop.relative_to(town.root)})\n\n{prop.read_text(encoding='utf-8')[:2500]}")
    if not parts:
        return ""
    return ("Compare each resident's checkpoint claims with the event log; when they disagree, file_flag to that resident with the "
            "evidence (a note to a colleague, not a verdict). Review each pending tool and open proposal for risk with review_item.\n\n"
            + "\n\n---\n\n".join(parts))


# -- running a round ----------------------------------------------------------------


class Context(Protocol):
    """What a round needs from the upkeep pass it runs in."""

    town: Town
    work_dir: Path
    # (resident, message, session key, session kind, access, exact commands): the step's own session in opencode
    agent_turn: Callable[..., Awaitable[str]]

    def log(self, line: str) -> None: ...


@dataclass
class StepRun:
    office: str
    resident: str = ""
    status: str = ""  # done | skipped | failed
    note: str = ""
    report: str = ""


@dataclass
class RoundRun:
    round: str
    trigger: str
    started: str = field(default_factory=now_iso)
    finished: str = ""
    status: str = ""  # done | failed
    steps: list[StepRun] = field(default_factory=lambda: cast(list[StepRun], []))


async def run(ctx: Context, r: Round, *, trigger: str, session_key: str,
              done: Callable[[str], bool] | None = None, mark: Callable[[str, dict[str, Any]], None] | None = None) -> RoundRun:
    """Run a round's steps in order. ``done``/``mark``: the pass's ledger, so an interrupted round resumes."""
    from babo.baton import notices

    record = RoundRun(r.name, trigger)
    previous = ""
    for index, step in enumerate(r.steps, 1):
        key = f"round:{r.name}:{index}"
        if done is not None and done(key):
            record.steps.append(StepRun(step.office, status="done", note="(done earlier in this pass)"))
            continue
        resident = step.who(ctx.town)
        if not resident:
            record.steps.append(StepRun(step.office, status="skipped", note="vacancy: nobody holds this office"))
            continue
        after: Callable[[], None] = lambda: None  # noqa: E731
        if step.material == "promotions":
            material, after = promotions_material(ctx.town)
        elif step.material == "inspection":
            material = inspection_material(ctx.town, ctx.work_dir)
        elif step.material == "previous":
            material = f"What the step before reported:\n\n{previous}" if previous else ""
        else:
            material = "-"
        if not material:
            record.steps.append(StepRun(step.office, resident, "skipped", "nothing to work on"))
            continue
        message = (f"{MARK} {r.title}, step {index} of {len(r.steps)} ({now_iso()}). Your duty: {step.duty}"
                   + ("" if material == "-" else f"\n\n{material}")
                   + "\n\nWhen you're done, say in a few lines what you did; it's the report the next step and the owner see.")
        try:
            report = await ctx.agent_turn(resident, message, session_key, f"round:{r.name}:{index}", dict(step.access), list(step.commands))
        except Exception as exc:  # the owner hears; the rest of the round waits for the next time
            record.steps.append(StepRun(step.office, resident, "failed", f"{type(exc).__name__}: {exc}"))
            record.status = "failed"
            notices.add(ctx.town, "round_failed", f"The round “{r.title}” stopped at step {index} ({resident}): {first_line(str(exc), 160)}",
                        resident=resident)
            ctx.log(f"round {r.name}: step {index} ({resident}) failed: {exc}")
            break
        after()
        previous = report.strip()
        record.steps.append(StepRun(step.office, resident, "done", report=previous[:4000]))
        if mark is not None:
            mark(key, {"resident": resident})
        ctx.log(f"round {r.name}: step {index} ({resident}): {first_line(previous, 120)}")
    record.finished = now_iso()
    record.status = record.status or "done"
    _record(ctx.town, record)
    return record


def _record(town: Town, record: RoundRun) -> None:
    path = town.state / "rounds" / "runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
    state = load_state(town)
    state.setdefault(record.round, {}).update(last_run=record.finished, last_status=record.status, last_trigger=record.trigger)
    _save_state(town, state)


def runs(town: Town, name: str | None = None, *, limit: int = 20) -> list[dict[str, Any]]:
    path = town.state / "rounds" / "runs.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = cast(dict[str, Any], json.loads(line))
        except ValueError:
            continue
        if name is None or item.get("round") == name:
            out.append(item)
    return out[-limit:][::-1]


# -- what the owner sees ----------------------------------------------------------------

BABO_OWN = (
    ("Memory reviews", "each resident reviews what it did into its own memory, when its activity has gone quiet"),
    ("Sessions wrap up", "a finished or left session is reviewed into memory, then frozen; a done-check runs first"),
    ("The records", "the ledger, the registry, the search index, a secret scrub and one commit for the town's history"),
    ("The clock", "upkeep waits for a free model, keeps quiet hours, and runs the rounds that are due"),
)


def _latest_rehearsal(town: Town, name: str) -> dict[str, Any] | None:
    from babo.baton import rehearsal

    try:
        latest = rehearsal.latest(town, name)
    except BatonError:
        return None
    return None if latest is None else {**asdict(latest), "proposal": latest.proposal()}


def overview(town: Town) -> dict[str, Any]:
    rounds, broken = load(town)
    state = load_state(town)
    rows: list[dict[str, Any]] = []
    for r in rounds:
        mine = state.get(r.name, {})
        last = _parse(mine.get("last_run")) or _parse(mine.get("created"))
        upcoming = next_scheduled(r, last) if last is not None else None
        rows.append({
            "name": r.name, "title": r.title, "about": r.about, "when": r.describe_when(), "runs": r.runs, "enabled": r.enabled,
            "steps": [{**asdict(s), "holder": s.who(town)} for s in r.steps],
            "last_run": mine.get("last_run", ""), "last_status": mine.get("last_status", ""),
            "next_run": upcoming.isoformat(timespec="minutes") if upcoming is not None else "",
            "recent": runs(town, r.name, limit=5),
            "rehearsal": _latest_rehearsal(town, r.name),
        })
    return {"rounds": rows, "broken": broken, "babo": [{"title": t, "what": w} for t, w in BABO_OWN]}



# -- running one by hand ----------------------------------------------------------------


def _requests_path(town: Town) -> Path:
    return town.state / "runtime" / "round-requests.json"


def request_run(town: Town, name: str) -> str:
    """Ask the upkeep service to run a round now (the web interface's *Run now*): it starts within a minute."""
    r = get(town, name)
    path = _requests_path(town)
    try:
        wanted = cast(list[str], json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        wanted = []
    if name not in wanted:
        wanted.append(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(wanted) + "\n")
    return f"“{r.title}” starts within a minute (the upkeep service runs it)."


def requested(town: Town) -> list[str]:
    try:
        data: object = json.loads(_requests_path(town).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(n) for n in cast(list[object], data)] if isinstance(data, list) else []


def clear_requests(town: Town) -> None:
    _requests_path(town).unlink(missing_ok=True)
