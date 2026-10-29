"""The town pages of the web interface: overview, review, notes, the town's
name, and the doctor. Reads are GET routes; changes arrive as allowlisted WebUI
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

from babo.baton.town import BatonError, Town
from babo.webui.http_utils import http_error, http_json_response

TOWN_MUTATION_PATHS = {
    "town.decide": "/api/town/decide",
    "town.note": "/api/town/note",
    "town.name": "/api/town/name",
}
_DECISION_KINDS = ("tool", "proposal", "founding", "flag")
_doctor_lock = asyncio.Lock()


def _town() -> Town:
    from babo.config.loader import load_config

    return Town.open(load_config().baton.town_path)


def _text(payload: dict[str, Any], key: str, limit: int = 4000) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise BatonError(f"missing {key}")
    if len(value) > limit:
        raise BatonError(f"{key} is longer than {limit} characters")
    return value


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

    async def _route(self, request: WsRequest, got: str) -> Response | None:
        from babo.baton import views

        if got == "/api/town":
            return http_json_response(await asyncio.to_thread(lambda: views.town_overview(_town())))
        if got == "/api/town/review":
            return http_json_response(await asyncio.to_thread(lambda: views.review_items(_town())))
        if got == "/api/town/doctor":
            return http_json_response(await self._doctor())
        if not self.is_mutation_path(got):
            return http_error(404, "Not found")
        payload = self._payload(request)
        if payload is None:
            return http_error(405, "Use the WebUI connection to change the town")
        if got == "/api/town/decide":
            kind = payload.get("kind")
            if kind not in _DECISION_KINDS:
                raise BatonError("kind must be tool, proposal, founding or flag")
            yes = payload.get("yes")
            if not isinstance(yes, bool):
                raise BatonError("missing yes")
            item_id, reason = _text(payload, "id", 400), _text(payload, "reason")
            message = await asyncio.to_thread(lambda: views.decide(_town(), cast(Any, kind), item_id, yes=yes, reason=reason))
        elif got == "/api/town/note":
            resident, text = _text(payload, "resident", 80), _text(payload, "text")
            message = await asyncio.to_thread(lambda: views.leave_note(_town(), resident, text))
        else:
            name = _text(payload, "name", 200)
            message = await asyncio.to_thread(lambda: views.name_town(_town(), name))
        return http_json_response({"message": message, "town": await asyncio.to_thread(lambda: views.town_overview(_town()))})

    async def _doctor(self) -> dict[str, Any]:
        from babo.baton.doctor import run_doctor
        from babo.config.loader import get_config_path, load_config

        if _doctor_lock.locked():
            raise BatonError("The doctor is already running; try again in a moment.")
        async with _doctor_lock:
            checks = await run_doctor(load_config(), config_path=get_config_path())
        return {"checks": [asdict(c) for c in checks], "ok": not any(c.status == "FAIL" for c in checks)}
