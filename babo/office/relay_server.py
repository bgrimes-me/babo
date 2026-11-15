"""The relay's HTTP glue: five routes, no judgement, and no default bind.

`relay.py` holds every decision the relay is allowed to make, and this module adds
none: each handler calls one method and returns its dict as the body, with 200 —
because the status code is a claim about this hop and the relay is answering.

Two values come from outside, and there are no defaults for either:

    BABO_OFFICE_BIND   the pod's own address, read from wg0 after it is up
    BABO_OFFICE_DOOR   the agent's local door, e.g. http://127.0.0.1:8900

The bind has no default on purpose. Loopback would make the relay unreachable by
the Manager — the only caller it has — and ``0.0.0.0`` is the public door this
house has ruled out. A relay nobody can reach is a pod nobody can ask about, so
the honest behaviour is to refuse to start and say which value is missing; the
supervisor turns that into a failed pod rather than a half-started one.

The port is not a second value: it is ``RELAY_PORT``, imported, because the
Manager reads the same constant.
"""

from __future__ import annotations

import os
from typing import Any, cast

from aiohttp import web

from babo.office.relay import RELAY_PORT, Relay

# The relay's own surface. These are not the agent's paths: `relay.py`'s
# AGENT_* constants are what the relay asks, and these are what the Manager asks.
RELAY_HEALTH = "/health"
RELAY_STATUS = "/status"
RELAY_QUIESCE = "/quiesce"
RELAY_FLUSH = "/flush"
RELAY_MESSAGE = "/message"

RELAY_KEY = web.AppKey[Relay]("relay")


class RelayConfigError(SystemExit):
    """The relay cannot start, and the sentence says what is missing."""


def bind_address(environ: dict[str, str] | None = None) -> str:
    """The address to listen on. Required, and never guessed."""
    values = os.environ if environ is None else environ
    address = values.get("BABO_OFFICE_BIND", "").strip()
    if not address:
        raise RelayConfigError(
            "relay: BABO_OFFICE_BIND is not set. There is no default bind: loopback "
            "would put the relay out of the Manager's reach, and 0.0.0.0 is the public "
            "door this house has ruled out. Write the pod's own address and start again."
        )
    return address


def door_url(environ: dict[str, str] | None = None) -> str:
    """Where the agent answers locally. Required, and never guessed."""
    values = os.environ if environ is None else environ
    door = values.get("BABO_OFFICE_DOOR", "").strip()
    if not door:
        raise RelayConfigError(
            "relay: BABO_OFFICE_DOOR is not set. The relay has to be told where the "
            "agent's local door is; it will not assume a port, because a wrong guess "
            "reads as 'the agent is not answering' forever."
        )
    return door


def _relay_of(request: web.Request) -> Relay:
    return request.app[RELAY_KEY]


async def handle_health(request: web.Request) -> web.Response:
    """GET /health — is the relay up. It asks nobody, so it answers immediately."""
    return web.json_response(_relay_of(request).health())


async def handle_status(request: web.Request) -> web.Response:
    """GET /status — where the agent's turns are, asked locally."""
    return web.json_response(_relay_of(request).status())


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        loaded = await request.json()
    except Exception:
        return {}
    return cast(dict[str, Any], loaded) if isinstance(loaded, dict) else {}


async def handle_quiesce(request: web.Request) -> web.Response:
    """POST /quiesce — stop admitting work and return where the turns stand."""
    body = await _body(request)
    reason = body.get("reason")
    return web.json_response(_relay_of(request).quiesce(str(reason) if reason else ""))


async def handle_flush(request: web.Request) -> web.Response:
    """POST /flush — persist and confirm."""
    body = await _body(request)
    reason = body.get("reason")
    return web.json_response(_relay_of(request).flush(str(reason) if reason else ""))


async def handle_message(request: web.Request) -> web.Response:
    """POST /message — deliver, and say only whether it was delivered."""
    body = await _body(request)
    attachments = body.get("attachments")
    if not isinstance(attachments, list):
        attachments = []
    return web.json_response(
        _relay_of(request).message(
            source=str(body.get("from") or ""),
            channel=str(body.get("channel") or ""),
            content=str(body.get("content") or ""),
            attachments=tuple(str(item) for item in attachments),
        )
    )


def create_app(relay: Relay) -> web.Application:
    """Five routes and nothing else: no auth, no TLS, no metrics, no static files."""
    app = web.Application()
    app[RELAY_KEY] = relay
    app.router.add_get(RELAY_HEALTH, handle_health)
    app.router.add_get(RELAY_STATUS, handle_status)
    app.router.add_post(RELAY_QUIESCE, handle_quiesce)
    app.router.add_post(RELAY_FLUSH, handle_flush)
    app.router.add_post(RELAY_MESSAGE, handle_message)
    return app


def main() -> None:
    """Start the relay on the address it was given. Fail loudly if it was not."""
    address = bind_address()
    relay = Relay(door_url())
    web.run_app(create_app(relay), host=address, port=RELAY_PORT, print=None)


if __name__ == "__main__":  # pragma: no cover - process entry point
    main()
