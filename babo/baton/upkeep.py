"""Upkeep: Babo's own work (memory reviews, the records) and the rounds, whenever they are due.

There is no fixed night. ``babo upkeep --watch`` (the ``babo-upkeep`` service)
checks every minute what is waiting and runs a pass (``babo.baton.night``)
when something is due and nobody is using the model:

- a resident's activity is waiting for its memory review, its session has
  gone quiet, and the oldest of it is ``review_after_hours`` old;
- a round is due (``babo.baton.rounds``): its trigger fired, or one of its
  steps has work waiting (the night round: notes to curate, tools or
  proposals to inspect). A round set to run when triggered doesn't wait for
  a free model.

"Nobody is using the model" means the model queue has served nothing but
upkeep for ``wait_minutes``. During ``quiet_hours`` everything waiting is done,
and housekeeping (index, ledger, secret scrub, commit, push) runs once a day
even when nobody talked. A catch-up (``babo upkeep --now``, or the button on
the Town page) does everything waiting at once, without waiting.

Work that a pass leaves unfinished (say, the Inspector didn't review a tool)
is not retried until ``retry_after_hours`` have passed, so upkeep never loops.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from loguru import logger

from babo.baton.baton_file import iter_checkpoints
from babo.baton.config import BatonConfig
from babo.baton.night import Night, read_offsets, unreviewed_events
from babo.baton.registry import Registry
from babo.baton.town import OWNER, BatonError, Town, atomic_write, now_iso

if TYPE_CHECKING:
    from babo.config.schema import Config


@dataclass
class Due:
    """What is waiting, and whether upkeep may do it now."""

    reasons: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)  # identifies the work, so unfinished work isn't retried at once
    residents: list[str] = field(default_factory=list)  # whose activity a pass would review
    sessions: list[str] = field(default_factory=list)  # "<resident>/<session>" opencode sessions to review and freeze
    rounds: list[tuple[str, str]] = field(default_factory=list)  # (round, why) the next pass runs
    waiting_for: str = ""  # why not now, in plain words ("" when it may run)

    @property
    def run(self) -> bool:
        return bool(self.reasons) and not self.waiting_for


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def in_quiet_hours(now: datetime, quiet_hours: str) -> bool:
    start_s, end_s = quiet_hours.split("-")
    start, end = time.fromisoformat(start_s), time.fromisoformat(end_s)
    current = now.timetz().replace(tzinfo=None)
    return start <= current < end if start <= end else current >= start or current < end


def _hours(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours:.0f} hour{'s' if round(hours) != 1 else ''}" if hours >= 1 else f"{delta.total_seconds() / 60:.0f} minutes"


def _state_path(town: Town):
    return town.state / "upkeep" / "scheduler.json"


def load_state(town: Town) -> dict[str, Any]:
    try:
        loaded: object = json.loads(_state_path(town).read_text(encoding="utf-8"))
        return cast(dict[str, Any], loaded) if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(town: Town, state: dict[str, Any]) -> None:
    atomic_write(_state_path(town), json.dumps(state, indent=2) + "\n")


def _request_path(town: Town):
    return town.state / "runtime" / "upkeep-request.json"


def request_catch_up(town: Town, *, by: str = OWNER) -> str:
    """Ask the running upkeep service to do everything waiting now (the web interface's button)."""
    atomic_write(_request_path(town), json.dumps({"at": now_iso(), "by": by}) + "\n")
    return "Catch-up requested: upkeep starts within a minute and does everything that is waiting."


def catch_up_requested(town: Town) -> bool:
    return _request_path(town).exists()


def due(town: Town, cfg: BatonConfig, *, now: datetime, model_idle_s: int | None, catch_up: bool = False,
        config: Config | None = None) -> Due:
    """``config`` (the whole config) adds residents' opencode sessions to look after."""
    up = cfg.upkeep
    quiet = in_quiet_hours(now, up.quiet_hours)
    out = Due()

    from babo.baton import opencode

    opencode.ingest_all(town)
    opencode.notify_waiting(town)
    if config is not None:
        opencode.refresh_running_now(town, config)  # new letters reach running residents at their next turn
        from babo.config.loader import get_config_path

        try:  # a finished task is checked before it may wrap up
            opencode.check_done(town, config, config_path=get_config_path().resolve())
        except (BatonError, OSError) as exc:
            logger.warning("Baton upkeep: done-checks: {}", exc)
    closing = opencode.needing_review(town, config, now=now) if config is not None else []
    for session, why in closing:
        out.sessions.append(f"{session.resident}/{session.id}")
        out.reasons.append(f"{session.resident}'s session “{session.title}” to review and freeze ({why})")
        out.keys.append(f"session:{session.resident}:{session.id}:{session.last_active}")

    activity, sizes = unreviewed_events(town, read_offsets(town))
    for resident, events in activity.items():
        times = [t for t in (_parse(str(e.get("at", ""))) for e in events) if t is not None]
        if not times:
            continue
        first, last = min(times), max(times)
        idle = timedelta(minutes=cfg.memory.idle_minutes)
        age = now - first
        session_over = now - last >= idle
        if catch_up or (session_over and (quiet or age >= timedelta(hours=up.review_after_hours))):
            out.residents.append(resident)
            out.reasons.append(f"{resident}: {_hours(age)} of activity to review")
            out.keys.append(f"review:{resident}:{sum(sizes[resident].values())}")

    # The town's work: rounds that are due (including those with work waiting).
    from babo.baton import rounds

    urgent = False
    asked = rounds.requested(town)
    for name in asked:  # run by hand: now, without waiting for a free model
        out.rounds.append((name, "run by hand"))
        out.reasons.append(f"round {name}, run by hand")
        out.keys.append(f"round:{name}:by-hand:{now.isoformat(timespec='minutes')}")
        urgent = True
    for r, why in rounds.due(town, now=now):
        if r.name in asked:
            continue
        out.rounds.append((r.name, why))
        out.reasons.append(why)
        out.keys.append(f"round:{r.name}:{why}")
        urgent = urgent or r.runs == "now"

    state = load_state(town)
    last = _parse(str(state.get("last_pass", "")))
    if quiet and (last is None or now - last >= timedelta(hours=20)) and not out.reasons:
        out.reasons.append("daily housekeeping")
        out.keys.append(f"housekeeping:{now.date().isoformat()}")

    if not out.reasons or catch_up:
        return out
    tried = _parse(str(state.get("last_tried", "")))
    skip_until = _parse(str(state.get("skip_until", "")))
    if skip_until is not None and now < skip_until:
        out.waiting_for = f"the model wasn't reachable; trying again at {skip_until:%H:%M}"
    elif (set(out.keys) == set(cast(list[str], state.get("last_keys", [])))
            and tried is not None and now - tried < timedelta(hours=up.retry_after_hours)):
        out.waiting_for = f"the same work was tried at {tried:%H:%M}; trying again after {up.retry_after_hours} hours"
    elif model_idle_s is None:
        out.waiting_for = "the model queue isn't answering"
    elif urgent:
        pass  # a round set to run when triggered doesn't wait for a free model (it runs at the lowest priority)
    elif model_idle_s < up.wait_minutes * 60:
        out.waiting_for = f"someone used the model in the last {up.wait_minutes} minutes"
    return out


async def model_idle_seconds(cfg: BatonConfig) -> int | None:
    """Seconds the model queue has served nothing but upkeep; None when it can't be asked."""
    url = f"http://{cfg.model.queue_host}:{cfg.model.queue_port}/baton/queue"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as client, client.get(url) as resp:
            data: object = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None
    idle = cast(dict[str, Any], data).get("idle_s") if isinstance(data, dict) else None
    return int(idle) if isinstance(idle, int) else None


def _unfinished_pass(town: Town) -> str | None:
    """A pass that stopped part-way (power cut, restart) is resumed, not started over."""
    for ledger in sorted((town.state / "upkeep").glob("*.json"), reverse=True):
        if ledger.name == "scheduler.json":
            continue
        try:
            steps = cast(dict[str, Any], json.loads(ledger.read_text(encoding="utf-8"))).get("steps", {})
        except (OSError, ValueError):
            continue
        return None if "postpass" in steps else ledger.stem
    return None


async def run_pass(config: Config, plan: Due, **kwargs: Any) -> dict[str, Any]:
    town = Town.open(config.baton.town_path)
    kwargs.setdefault("night", _unfinished_pass(town))
    result = await Night(config, residents=plan.residents, sessions=plan.sessions, rounds=plan.rounds, **kwargs).run()
    state = load_state(town)
    if result.get("status") == "ok":
        state.update({"last_pass": now_iso(), "last_tried": now_iso(), "last_keys": plan.keys, "last_reasons": plan.reasons,
                      "last_result": {k: v for k, v in result.items() if k in ("night", "committed", "ran")}})
        state.pop("skip_until", None)
    else:  # the model wasn't reachable: look again in a while, not every minute
        state["skip_until"] = (datetime.now().astimezone() + timedelta(minutes=15)).isoformat(timespec="seconds")
    _save_state(town, state)
    return result


async def run_now(config: Config, **kwargs: Any) -> dict[str, Any] | None:
    """Do everything that is waiting, now (`babo upkeep --now`). None when nothing was waiting."""
    town = Town.open(config.baton.town_path)
    plan = due(town, config.baton, now=datetime.now().astimezone(), model_idle_s=None, catch_up=True, config=config)
    if not plan.reasons:
        return None
    return await run_pass(config, plan, **kwargs)


def _heartbeat(town: Town, plan: Due, *, running: bool) -> None:
    atomic_write(town.state / "runtime" / "upkeep.json",
                 json.dumps({"at": now_iso(), "running": running, **asdict(plan)}, indent=2) + "\n")


async def watch(config: Config) -> None:
    """The upkeep service: check every minute, run a pass when one is due."""
    from babo.baton import requests
    from babo.config.loader import get_config_path

    cfg = config.baton
    town = Town.open(cfg.town_path)
    logger.info("Babo upkeep: watching {} (quiet hours {})", town.root, cfg.upkeep.quiet_hours)
    for interrupted in requests.recover_interrupted(town):
        logger.warning("Babo upkeep: session request {} was interrupted by a restart", interrupted)
    request_job: asyncio.Future[Any] | None = None
    while True:
        try:
            # Requests the owner approved run one at a time, beside upkeep, not instead of it.
            if request_job is None or request_job.done():
                approved = requests.next_approved(town)
                if approved is not None:
                    logger.info("Babo upkeep: running session request {} ({})", approved.id, approved.resident)
                    request_job = asyncio.ensure_future(asyncio.to_thread(
                        requests.run, town, config, approved, config_path=get_config_path().resolve()))
            catch_up = catch_up_requested(town)
            plan = due(town, cfg, now=datetime.now().astimezone(), model_idle_s=await model_idle_seconds(cfg), catch_up=catch_up,
                       config=config)
            if catch_up:
                _request_path(town).unlink(missing_ok=True)
            if plan.run and plan.rounds:
                from babo.baton import rounds

                rounds.clear_requests(town)
            _heartbeat(town, plan, running=plan.run)
            if plan.run:
                logger.info("Babo upkeep: {}", "; ".join(plan.reasons))
                result = await run_pass(config, plan)
                if result.get("status") != "ok":
                    logger.warning("Babo upkeep: pass {}: {}", result.get("status"), result.get("reason", ""))
                _heartbeat(town, plan, running=False)
        except Exception:
            logger.exception("Babo upkeep: a check failed; trying again in a minute")
        await asyncio.sleep(cfg.upkeep.check_every_seconds)


def status(town: Town, cfg: BatonConfig) -> dict[str, Any]:
    """What upkeep is doing, for `babo upkeep` and the Town page."""
    beat: dict[str, Any] = {}
    try:
        loaded: object = json.loads((town.state / "runtime" / "upkeep.json").read_text(encoding="utf-8"))
        beat = cast(dict[str, Any], loaded) if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        pass
    seen = _parse(str(beat.get("at", "")))
    alive = seen is not None and datetime.now().astimezone() - seen < timedelta(seconds=cfg.upkeep.check_every_seconds * 3 + 60)
    state = load_state(town)
    reviews = {r.id: max((c.at for c in iter_checkpoints(town.resident_dir(r.id)) if c.trigger.startswith("fold ")), default="")
               for r in Registry(town).all() if r.status == "active"}
    return {
        "service": "running" if alive else "not running",
        "running": bool(beat.get("running")) and alive,
        "waiting": cast(list[str], beat.get("reasons", [])) if alive else [],
        "waiting_for": str(beat.get("waiting_for", "")) if alive else "",
        "catch_up_requested": catch_up_requested(town),
        "last_pass": str(state.get("last_pass", "")),
        "last_result": state.get("last_result"),
        "last_reviews": reviews,
        "quiet_hours": cfg.upkeep.quiet_hours,
        "wait_minutes": cfg.upkeep.wait_minutes,
    }
