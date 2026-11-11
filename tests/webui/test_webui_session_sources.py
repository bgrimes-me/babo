"""Operator sessions behind a trusted network source: issue, refuse, revoke, restart.

The WireGuard source rule is the outer authentication (Router side). This module
covers the inner half: an operator arriving from an explicitly trusted peer
address may obtain the same UI session a loopback operator gets, that peer
matching is exact (no headers, no prefixes), and revocation is real — by
operator action and by process restart.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from websockets.datastructures import Headers
from websockets.http11 import Request

from babo.channels.websocket.runtime import WebSocketConfig
from babo.webui.gateway_tokens import GatewayTokenStore
from babo.webui.http_utils import is_trusted_source_request
from babo.webui.ws_http import GatewayHTTPHandler


def test_trusted_sources_take_exact_addresses_only() -> None:
    assert WebSocketConfig(trusted_sources=["10.0.0.2/32"]).trusted_sources == ["10.0.0.2/32"]
    for bad in ("10.0.0.0/8", "0.0.0.0/0", "10.0.0.2", "example.com", "::/0", "::1/128"):
        with pytest.raises(ValueError):
            WebSocketConfig(trusted_sources=[bad])


def test_only_the_exact_peer_is_trusted_and_headers_never_count() -> None:
    trusted = ["10.0.0.2/32"]
    assert is_trusted_source_request(SimpleNamespace(remote_address=("10.0.0.2", 5000)), trusted)
    assert is_trusted_source_request(SimpleNamespace(remote_address=("::ffff:10.0.0.2", 5000)), trusted)
    assert not is_trusted_source_request(SimpleNamespace(remote_address=("10.0.0.20", 5000)), trusted)
    assert not is_trusted_source_request(SimpleNamespace(remote_address=("10.0.0.99", 5000)), trusted)
    assert not is_trusted_source_request(SimpleNamespace(remote_address=None), trusted)


def _bootstrap(handler: GatewayHTTPHandler, peer: str, headers: dict[str, str] | None = None) -> int:
    connection = SimpleNamespace(remote_address=(peer, 5000))
    request = Request("/webui/bootstrap", Headers({"Host": "10.0.0.2:18790", **(headers or {})}))
    return handler._handle_bootstrap(connection, request).status_code


def _handler(trusted: list[str]) -> GatewayHTTPHandler:
    handler = object.__new__(GatewayHTTPHandler)
    handler.config = WebSocketConfig(trusted_sources=trusted)
    handler.tokens = GatewayTokenStore()
    handler._log = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                                   error=lambda *a, **k: None, exception=lambda *a, **k: None)
    handler.ingress = SimpleNamespace(bootstrap_limits=lambda **kwargs: {})
    handler.runtime_model_name = ""
    handler.settings = SimpleNamespace(config=SimpleNamespace(path=""))
    handler._runtime_surface = "webui"
    handler._capabilities = {}
    return handler


def test_a_trusted_peer_gets_a_session_and_an_untrusted_one_gets_nothing() -> None:
    handler = _handler(["10.0.0.2/32"])
    assert _bootstrap(handler, "10.0.0.2") == 200
    issued = len(handler.tokens.issued_tokens), len(handler.tokens.api_tokens)
    assert issued == (1, 1), "a trusted operator gets both the socket and API tokens"
    before = sum(issued)
    assert _bootstrap(handler, "10.0.0.99") == 403
    assert _bootstrap(handler, "10.0.0.99", {"X-Forwarded-For": "10.0.0.2"}) == 403
    assert sum((len(handler.tokens.issued_tokens), len(handler.tokens.api_tokens))) == before


def test_revoking_sessions_kills_the_token_that_asked_for_it() -> None:
    handler = _handler(["10.0.0.2/32"])
    assert _bootstrap(handler, "10.0.0.2") == 200
    api_token = next(iter(handler.tokens.api_tokens))
    request = Request("/api/office", Headers({"Authorization": f"Bearer {api_token}"}))
    assert handler.tokens.check_api_token(request)
    response = handler._handle_tokens_revoke(SimpleNamespace())
    assert json.loads(response.body)["revoked"] == 2
    assert not handler.tokens.api_tokens and not handler.tokens.issued_tokens
    assert not handler.tokens.check_api_token(request), "the revoked token must not work again"


def test_a_restart_invalidates_every_session() -> None:
    before = GatewayTokenStore()
    token = before.issue_api_token(3600)
    after = GatewayTokenStore()  # a new gateway process: new instance, empty store
    assert after.instance_id != before.instance_id
    request = Request("/api/office", Headers({"Authorization": f"Bearer {token}"}))
    assert not after.check_api_token(request)


def test_a_trusted_listener_keeps_its_rendered_address_and_gets_no_static_route() -> None:
    """`babo webui` must not rewrite a deliberate bind to loopback, nor mint a
    token_issue_secret that the trusted-source listener refuses to start with."""
    from babo.cli.webui_support import _ensure_local_webui_channel
    from babo.config.schema import Config

    cfg = Config.model_validate({
        "channels": {"websocket": {"enabled": True, "host": "10.0.0.5", "port": 18790,
                                   "trusted_sources": ["10.0.0.2/32"]}},
    })
    changed = _ensure_local_webui_channel(cfg, port=18790, yes=True)
    ws = cfg.channels.websocket
    assert ws["host"] == "10.0.0.5", "the deliberate bind must survive setup"
    assert ws.get("token_issue_secret", "") == "", "no static issuance route on a trusted listener"
    assert ws.get("token_issue_path", "") == ""
    assert ws["trusted_sources"] == ["10.0.0.2/32"]
    assert WebSocketConfig.model_validate(ws).websocket_requires_token is True, (
        "a session token is still required for the handshake (the default, not overridden)")
    assert changed is False, "a complete trusted-listener config needs no first-run mutation"


def test_api_routes_do_not_accept_a_token_in_the_query_string() -> None:
    store = GatewayTokenStore()
    token = store.issue_api_token(3600)
    header = Request("/api/office", Headers({"Authorization": f"Bearer {token}"}))
    assert store.check_api_token(header)
    in_url = Request(f"/api/office?token={token}", Headers({}))
    assert not store.check_api_token(in_url), "/api must not authenticate from a URL"
    # The WebSocket handshake keeps its query carriage: that is its only channel.
    store2 = GatewayTokenStore()
    ws_token = store2.issue_api_token(3600)
    ws = Request(f"/ws?token={ws_token}", Headers({}))
    assert store2.check_api_token(ws)


def test_the_listeners_unix_socket_is_local_and_needs_no_trust_entry() -> None:
    """The in-pod operator door: a 0600 Unix socket, not a network peer.

    fettle's served run found this seam — in a pod the CLI cannot reach the
    listener over loopback (nothing is bound there) and is not a trusted source
    (the only trusted address is the host's). The socket is local by
    construction, so it needs neither.
    """
    from babo.webui.http_utils import is_unix_socket_peer

    handler = object.__new__(GatewayHTTPHandler)
    handler.config = WebSocketConfig(trusted_sources=["10.0.0.2/32"], unix_socket_path="/run/babo/ui.sock")
    handler.tokens = GatewayTokenStore()
    handler._log = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                                   error=lambda *a, **k: None, exception=lambda *a, **k: None)
    handler.ingress = SimpleNamespace(bootstrap_limits=lambda **kwargs: {})
    handler.runtime_model_name = ""
    handler.settings = SimpleNamespace(config=SimpleNamespace(path=""))
    handler._runtime_surface = "webui"
    handler._capabilities = {}

    unix_peer = SimpleNamespace(remote_address=None)
    assert is_unix_socket_peer(unix_peer)
    request = Request("/webui/bootstrap", Headers({"Host": "localhost"}))
    assert handler._handle_bootstrap(unix_peer, request).status_code == 200
    assert handler.tokens.api_tokens, "the in-pod command gets a session without a network trust entry"

    # A TCP peer is never mistreated as the socket, and the same config still
    # refuses a source that is not on the list.
    tcp_peer = SimpleNamespace(remote_address=("10.0.0.99", 5000))
    assert not is_unix_socket_peer(tcp_peer)
    assert handler._handle_bootstrap(tcp_peer, request).status_code == 403

    # With no socket configured, "no remote address" does not become a free pass.
    handler.config = WebSocketConfig(trusted_sources=["10.0.0.2/32"])
    assert handler._handle_bootstrap(unix_peer, request).status_code == 403
