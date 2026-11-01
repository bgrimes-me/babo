"""The town pages of the web interface: overview, review, notes, the town's
name, profiles, settings and the doctor. Reads are GET routes; changes arrive as allowlisted WebUI
mutations. Everything goes through ``babo.baton.views``, the same functions
`babo town`, `babo review` and `babo note` use."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict
from typing import Any, cast

from loguru import logger
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from babo.baton.config import BatonConfig
from babo.baton.town import BatonError, Town
from babo.webui.http_utils import http_error, http_json_response

TOWN_MUTATION_PATHS = {
    "town.decide": "/api/town/decide",
    "town.note": "/api/town/note",
    "town.name": "/api/town/name",
    "town.settings": "/api/town/settings/update",
    "town.profile": "/api/town/profiles/update",
    "town.upkeep": "/api/town/upkeep/now",
    "town.session.new": "/api/town/sessions/new",
    "town.session.resume": "/api/town/sessions/resume",
    "town.session.freeze": "/api/town/sessions/freeze",
    "town.session.remove": "/api/town/sessions/remove",
    "town.session.send": "/api/town/sessions/send",
    "town.session.stop": "/api/town/sessions/stop",
    "town.session.checkpoint": "/api/town/sessions/checkpoint",
    "town.session.model": "/api/town/sessions/model",
    "town.session.answer": "/api/town/sessions/answer",
    "town.session.link": "/api/town/sessions/link",
    "town.session.archive": "/api/town/sessions/archive",
    "town.session.unarchive": "/api/town/sessions/unarchive",
    "town.session.organize": "/api/town/sessions/organize",
    "town.session.access": "/api/town/sessions/access",
    "town.notices.read": "/api/town/notices/read",
    "town.allow_all": "/api/town/allow-all/update",
    "town.eval": "/api/town/eval/run",
    "town.rounds.run": "/api/town/rounds/run",
    "town.rounds.set": "/api/town/rounds/set",
    "town.rounds.rehearse": "/api/town/rounds/rehearse",
    "town.rounds.approve": "/api/town/rounds/approve",
    "town.rounds.access": "/api/town/rounds/access",
}
_DECISION_KINDS = ("tool", "proposal", "move_in", "flag", "errand", "founding", "session")  # the last two: old names (step 23)
_doctor_lock = asyncio.Lock()


def _town() -> Town:
    from babo.config.loader import load_config

    return Town.open(load_config().baton.town_path)


def _baton() -> BatonConfig:
    from babo.config.loader import load_config

    return load_config().baton


def _text(payload: dict[str, Any], key: str, limit: int = 4000) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise BatonError(f"missing {key}")
    if len(value) > limit:
        raise BatonError(f"{key} is longer than {limit} characters")
    return value


def _update_settings(payload: dict[str, Any]) -> dict[str, Any]:
    from babo.baton import settings as town_settings
    from babo.config.loader import get_config_path, load_config

    changes = payload.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise BatonError("missing changes")
    said = town_settings.update(get_config_path(), cast(dict[str, object], changes))
    return {"message": "; ".join(said) if said else "Nothing changed.",
            "settings": town_settings.current(load_config().baton)}


def _profiles() -> dict[str, Any]:
    from babo.baton import views
    from babo.baton.gateway import presets_overview
    from babo.config.loader import load_config

    cfg = load_config()
    return {"profiles": views.profiles_overview(Town.open(cfg.baton.town_path), cfg.baton, sorted(cfg.tools.mcp_servers)),
            "presets": presets_overview(cfg)}


def _update_profile(agent: str, changes: dict[str, object]) -> str:
    from babo.baton import views
    from babo.config.loader import load_config

    cfg = load_config()
    from babo.baton.gateway import preset_names

    return views.update_profile(Town.open(cfg.baton.town_path), cfg.baton, agent, changes, sorted(cfg.tools.mcp_servers), preset_names(cfg))


def _review() -> dict[str, Any]:
    from babo.baton import views
    from babo.config.loader import load_config

    cfg = load_config()
    return views.review_items(Town.open(cfg.baton.town_path), cfg)


def _pending_requests() -> dict[str, Any]:
    from babo.baton import requests as session_requests
    from babo.config.loader import load_config

    cfg = load_config()
    town = Town.open(cfg.baton.town_path)
    return {"pending": [session_requests.describe(town, cfg, r) for r in session_requests.pending(town)]}


def _sessions(archived: bool = False) -> dict[str, Any]:
    from babo.baton import views
    from babo.config.loader import load_config

    cfg = load_config()
    return views.sessions_overview(Town.open(cfg.baton.town_path), cfg, archived=archived)


def _eval_status() -> dict[str, Any]:
    from babo.baton import evals
    from babo.baton.gateway import preset_names
    from babo.config.loader import load_config

    cfg = load_config()
    return evals.background_status(Town.open(cfg.baton.town_path), preset_names(cfg))


def _start_eval(preset: str, baseline_too: bool) -> str:
    """`babo eval --preset <preset>` from the web interface: in the background, with every task."""
    from babo.baton import evals
    from babo.baton.gateway import Router, preset_names
    from babo.config.loader import load_config

    cfg = load_config()
    if preset not in preset_names(cfg):
        raise BatonError(f"There's no model preset called '{preset}'.")
    return evals.start_background(Town.open(cfg.baton.town_path), cfg.baton.model, preset,
                                  real_model=Router(cfg).resolve(preset).model, baseline_too=baseline_too)


def _session_change(action: str, payload: dict[str, Any]) -> str:
    from babo.baton import views
    from babo.config.loader import get_config_path, load_config

    cfg = load_config()
    town = Town.open(cfg.baton.town_path)
    resident = _text(payload, "resident", 80)
    if action == "new":
        overrides: object = payload.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise BatonError("overrides must be an object")
        title = payload.get("title") if isinstance(payload.get("title"), str) else ""
        task = payload.get("task") if isinstance(payload.get("task"), str) else ""
        done_check = payload.get("done_check") if isinstance(payload.get("done_check"), str) else ""
        return views.start_session(town, cfg, resident, title=cast(str, title)[:200], overrides=cast(dict[str, object], overrides),
                                   config_path=get_config_path().resolve(), task=cast(str, task)[:20000],
                                   done_check=cast(str, done_check)[:2000])
    session_id = _text(payload, "id", 80)
    config_path = get_config_path().resolve()
    if action == "send":
        return views.send_to_session(town, cfg, resident, session_id, _text(payload, "text", 20000), config_path=config_path)
    if action == "stop":
        return views.stop_session_turn(town, resident, session_id)
    if action == "checkpoint":
        return views.checkpoint_session(town, cfg, resident, session_id, config_path=config_path)
    if action == "model":
        preset = payload.get("preset") if isinstance(payload.get("preset"), str) else ""
        return views.set_session_model(town, cfg, resident, session_id, cast(str, preset), config_path=config_path)
    if action == "archive" or action == "unarchive":
        return views.archive_session(town, resident, session_id, archived=action == "archive")
    if action == "access":
        changes: object = payload.get("changes") or {}
        if not isinstance(changes, dict) or not all(isinstance(v, str) for v in cast(dict[str, object], changes).values()):
            raise BatonError("changes must be an object of settings")
        return views.change_session_access(town, cfg, resident, session_id, cast(dict[str, str], changes),
                                           config_path=config_path, everything=payload.get("all") is True)
    if action == "organize":
        title, pinned, tags = payload.get("title"), payload.get("pinned"), payload.get("tags")
        if tags is not None and not (isinstance(tags, list) and all(isinstance(t, str) for t in cast(list[object], tags))):
            raise BatonError("tags must be a list of words")
        return views.organize_session(
            town, resident, session_id,
            title=title if isinstance(title, str) else None,
            pinned=pinned if isinstance(pinned, bool) else None,
            tags=cast(list[str], tags) if tags is not None else None,
        )
    if action == "answer":
        return views.answer_session_question(town, resident, session_id, _text(payload, "request", 120), _text(payload, "decision", 10))
    if action == "resume":
        return views.resume_session(town, cfg, resident, session_id, config_path=get_config_path().resolve())
    if action == "remove":
        return views.remove_session(town, resident, session_id)
    return views.freeze_session(town, cfg, resident, session_id, config_path=get_config_path().resolve())


class TownRoutes:
    def __init__(self, *, authorized: Callable[[WsRequest], bool], payload: Callable[[WsRequest], dict[str, Any] | None]) -> None:
        self._authorized = authorized
        self._payload = payload

    @staticmethod
    def is_mutation_path(path: str) -> bool:
        return path in TOWN_MUTATION_PATHS.values()

    async def dispatch(self, request: WsRequest, got: str) -> Response | None:
        if got != "/api/town" and not got.startswith("/api/town/"):
            return None
        if not self._authorized(request):
            return http_error(401, "Unauthorized")
        try:
            return await self._route(request, got)
        except BatonError as exc:
            return http_error(400, str(exc))
        except OSError:
            logger.exception("town route {} failed", got)
            return http_error(500, "The town's files could not be read or written; see the gateway log.")
        except Exception as exc:
            # Never let the page fall back to the WebSocket server's bare error: say what broke.
            logger.exception("town route {} failed", got)
            return http_error(500, f"Babo couldn't load this ({type(exc).__name__}: {exc}). If Babo was just upgraded, "
                                   "restart it: on the host, systemctl --user restart babo-queue babo-gateway babo-upkeep")

    async def _route(self, request: WsRequest, got: str) -> Response | None:
        from babo.baton import views

        if got == "/api/town":
            return http_json_response(await asyncio.to_thread(lambda: views.town_overview(_town(), _baton())))
        if got == "/api/town/review":
            return http_json_response(await asyncio.to_thread(lambda: _review()))
        if got == "/api/town/doctor":
            return http_json_response(await self._doctor())
        if got == "/api/town/settings":
            from babo.baton import settings as town_settings
            from babo.config.loader import load_config

            return http_json_response({"settings": town_settings.current(load_config().baton)})
        if got in ("/api/town/errands", "/api/town/session-requests"):  # (the second: the old path, until step 23)
            return http_json_response(await asyncio.to_thread(_pending_requests))
        if got == "/api/town/allow-all":
            from babo.baton import allow_all

            return http_json_response(await asyncio.to_thread(lambda: allow_all.status(_town())))
        if got == "/api/town/sessions":
            from babo.webui.http_utils import parse_query

            archived = parse_query(request.path).get("archived", [""])[0] in ("1", "true")
            return http_json_response(await asyncio.to_thread(lambda: _sessions(archived)))
        if got == "/api/town/notices":
            from babo.baton import notices

            return http_json_response({"notices": await asyncio.to_thread(lambda: notices.recent(_town(), unread_only=True))})
        if got == "/api/town/profiles":
            return http_json_response(await asyncio.to_thread(_profiles))
        if got == "/api/town/rounds":
            from babo.baton import rounds

            return http_json_response(await asyncio.to_thread(lambda: rounds.overview(_town())))
        if got == "/api/town/guides":
            from babo.baton import guides

            return http_json_response({"guides": await asyncio.to_thread(lambda: guides.stats(_town()))})
        if got == "/api/town/eval":
            return http_json_response(await asyncio.to_thread(_eval_status))
        if not self.is_mutation_path(got):
            return http_error(404, "Not found")
        payload = self._payload(request)
        if payload is None:
            return http_error(405, "Use the WebUI connection to change the town")
        if got == "/api/town/decide":
            kind = payload.get("kind")
            if kind not in _DECISION_KINDS:
                raise BatonError("kind must be tool, proposal, move_in, flag or errand")
            yes = payload.get("yes")
            if not isinstance(yes, bool):
                raise BatonError("missing yes")
            item_id, reason = _text(payload, "id", 400), _text(payload, "reason")
            digest = payload.get("digest")
            if kind in ("errand", "session") and not isinstance(digest, str):
                raise BatonError("missing digest: approve an errand from what you were shown")
            message = await asyncio.to_thread(lambda: views.decide(_town(), cast(Any, kind), item_id, yes=yes, reason=reason,
                                                                   digest=digest if isinstance(digest, str) else None))
        elif got == "/api/town/note":
            resident, text = _text(payload, "resident", 80), _text(payload, "text")
            message = await asyncio.to_thread(lambda: views.leave_note(_town(), resident, text))
        elif got == "/api/town/profiles/update":
            agent, changes = _text(payload, "agent", 80), payload.get("changes")
            if not isinstance(changes, dict) or not changes:
                raise BatonError("missing changes")
            message = await asyncio.to_thread(lambda: _update_profile(agent, cast(dict[str, object], changes)))
            return http_json_response({"message": message, **await asyncio.to_thread(_profiles)})
        elif got == "/api/town/allow-all/update":
            from babo.baton import allow_all

            key = _text(payload, "key", allow_all.MAX_KEY)
            on, hours = payload.get("on"), payload.get("hours")
            if not isinstance(on, bool):
                raise BatonError("missing on")
            if on and hours is not None and (isinstance(hours, bool) or not isinstance(hours, int | float)):
                raise BatonError("hours must be a number, or null for until you turn it off")
            message = await asyncio.to_thread(lambda: allow_all.turn_on(_town(), key, hours=float(hours) if hours is not None else None)
                                              if on else allow_all.turn_off(_town(), key))
            return http_json_response({"message": message, **await asyncio.to_thread(lambda: allow_all.status(_town()))})
        elif got == "/api/town/sessions/link":
            from babo.baton import views as town_views

            resident, session_id = _text(payload, "resident", 80), _text(payload, "id", 80)
            link = await asyncio.to_thread(lambda: town_views.session_link(_town(), resident, session_id))
            return http_json_response({"link": link})
        elif got == "/api/town/notices/read":
            from babo.baton import notices

            ids = payload.get("ids")
            chosen = [str(i) for i in cast(list[object], ids)] if isinstance(ids, list) else None
            changed = await asyncio.to_thread(lambda: notices.mark_read(_town(), chosen))
            return http_json_response({"read": changed, "notices": await asyncio.to_thread(lambda: notices.recent(_town(), unread_only=True))})
        elif got.startswith("/api/town/sessions/"):
            message = await asyncio.to_thread(lambda: _session_change(got.rsplit("/", 1)[-1], payload))
            return http_json_response({"message": message, **await asyncio.to_thread(_sessions)})
        elif got in ("/api/town/rounds/run", "/api/town/rounds/set", "/api/town/rounds/rehearse", "/api/town/rounds/approve",
                     "/api/town/rounds/access"):
            from babo.baton import rehearsal, rounds

            name = _text(payload, "name", 80)
            if got.endswith("/rehearse"):
                message = await asyncio.to_thread(lambda: rehearsal.request(_town(), name, live=payload.get("live") is True))
            elif got.endswith("/approve"):
                rehearsal_id = _text(payload, "rehearsal", 200)
                message = await asyncio.to_thread(lambda: rehearsal.approve(_town(), rehearsal_id))
            elif got.endswith("/access"):
                step, access, commands = payload.get("step"), cast(object, payload.get("access") or {}), payload.get("commands")
                if not isinstance(step, int) or not isinstance(access, dict):
                    raise BatonError("missing step or access")
                if commands is not None and not (isinstance(commands, list) and all(isinstance(c, str) for c in cast(list[object], commands))):
                    raise BatonError("commands must be a list of exact commands")
                changes = {str(k): str(v) for k, v in cast(dict[object, object], access).items()}
                message = await asyncio.to_thread(lambda: rehearsal.set_step_access(
                    _town(), name, step, changes, cast(list[str], commands) if commands is not None else None))
            elif got.endswith("/run"):
                message = await asyncio.to_thread(lambda: rounds.request_run(_town(), name))
            else:
                enabled, runs = payload.get("enabled"), payload.get("runs")
                changed = await asyncio.to_thread(lambda: rounds.set_options(
                    _town(), name, enabled=enabled if isinstance(enabled, bool) else None, runs=runs if isinstance(runs, str) else None))
                message = f"“{changed.title}” is {'on' if changed.enabled else 'off'}, and " + (
                    "waits for a free model." if changed.runs == "wait" else "runs when triggered.")
            return http_json_response({"message": message, **await asyncio.to_thread(lambda: rounds.overview(_town()))})
        elif got == "/api/town/eval/run":
            preset = _text(payload, "preset", 120)
            baseline_too = payload.get("baseline") is True
            message = await asyncio.to_thread(lambda: _start_eval(preset, baseline_too))
            return http_json_response({"message": message, **await asyncio.to_thread(_eval_status)})
        elif got == "/api/town/upkeep/now":
            message = await asyncio.to_thread(lambda: views.request_upkeep(_town()))
        elif got == "/api/town/name":
            name = _text(payload, "name", 200)
            message = await asyncio.to_thread(lambda: views.name_town(_town(), name))
        else:
            return http_json_response(await asyncio.to_thread(lambda: _update_settings(payload)))
        return http_json_response({"message": message, "town": await asyncio.to_thread(lambda: views.town_overview(_town(), _baton()))})

    async def _doctor(self) -> dict[str, Any]:
        from babo.baton.doctor import run_doctor
        from babo.config.loader import get_config_path, load_config

        if _doctor_lock.locked():
            raise BatonError("The doctor is already running; try again in a moment.")
        async with _doctor_lock:
            checks = await run_doctor(load_config(), config_path=get_config_path())
        return {"checks": [asdict(c) for c in checks], "ok": not any(c.status == "FAIL" for c in checks)}
