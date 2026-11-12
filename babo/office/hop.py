"""The UI's client for the Manager hop (D-12: separate pods, UI→Manager only).

The UI holds the operator surface and the session store; the Manager holds
lifecycle, credentials and the runtime. This module is the *only* way a
lifecycle request leaves the UI pod, and it is deliberately small: four calls,
one timeout, no retries, no callbacks.

Two rules that shape the code more than the endpoints do:

- **A failure is a value, not an exception.** A transport problem, a 503 or a
  garbage body all come back as ``HopResult(ok=False, said=…)`` so the page can
  render ``unavailable``/``unknown`` without a try/except deciding what "empty"
  means. An exception here would push that judgement onto every caller.
- **The Manager's words are the verdict.** This client never invents a terminal
  state: ``pending``/``running`` and the closed terminal set pass through
  untouched.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, cast

from babo.office.actors import Actor
from babo.office.api import AGENT_ACTIONS, OPERATOR_ACTIONS, OPERATOR_ACTOR, OPERATOR_SOURCE


@dataclass(frozen=True)
class HopResult:
    """One answer from the Manager: a status, a payload, and its own words."""

    ok: bool
    status: int = 0
    payload: dict[str, Any] = field(default_factory=lambda: cast(dict[str, Any], {}))
    said: str = ""

    @property
    def unavailable(self) -> bool:
        return not self.ok


Transport = Callable[[str, str, dict[str, Any] | None, float], tuple[int, str]]


def _urllib_transport(method: str, url: str, body: dict[str, Any] | None, timeout_s: float) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace") if exc.fp else ""


@dataclass
class HopClient:
    """Four calls, one direction. Construct with the Manager's base URL."""

    base_url: str
    timeout_s: float = 10.0
    transport: Transport = _urllib_transport
    name: str = "hop"

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> HopResult:
        url = self.base_url.rstrip("/") + path
        try:
            status, text = self.transport(method, url, body, self.timeout_s)
        except Exception as exc:  # transport is outside our control; report, never raise
            return HopResult(ok=False, said=f"manager unreachable: {exc}")
        payload: dict[str, Any] = {}
        if text.strip():
            try:
                loaded: object = json.loads(text)
            except ValueError:
                return HopResult(ok=False, status=status, said=f"the manager answered {status} with non-JSON")
            if isinstance(loaded, dict):
                payload = cast(dict[str, Any], loaded)
        if status == 200 or status == 202:
            return HopResult(ok=True, status=status, payload=payload)
        said = str(payload.get("detail") or payload.get("said") or text.strip()[:300] or f"status {status}")
        return HopResult(ok=False, status=status, payload=payload, said=said)

    # -- the surface ------------------------------------------------------
    def health(self) -> HopResult:
        return self._call("GET", "/api/v1/office/health")

    def agents(self) -> HopResult:
        return self._call("GET", "/api/v1/office/agents")

    def act(self, handle: str, action: str, reason: str, *, actor: Actor,
            action_id: str | None = None) -> HopResult:
        if action in OPERATOR_ACTIONS:
            # The generic path is the agent path: it carries agent verbs and agent
            # handles. The pod verb has its own method, because its actor is not
            # the page's to choose (ruled 2026-11-11).
            return HopResult(ok=False, said=(f"{action} is not carried on the agent path: use the operator "
                                             f"door (host-WG session, {OPERATOR_SOURCE!r})"))
        if action not in AGENT_ACTIONS:
            # Refuse locally too: a typo should not become a network round trip
            # whose 400 the page then has to translate.
            return HopResult(ok=False, said=f"unknown action {action!r}; it is one of {', '.join(AGENT_ACTIONS)}")
        body: dict[str, Any] = {"handle": handle, "action": action, "reason": reason,
                                "actor": actor.name, "actor_source": actor.source}
        if action_id:
            body["action_id"] = action_id
        return self._call("POST", "/api/v1/office/actions", body)

    def action(self, action_id: str) -> HopResult:
        return self._call("GET", f"/api/v1/office/actions/{action_id}")

    def restart_ui(self, reason: str, *, actor: Actor, action_id: str | None = None) -> HopResult:
        """The one verb whose actor is not the page's to choose (ruled 2026-11-11).

        The UI derives the operator from the **authenticated host-WG session** and
        sends that actor; it never accepts one from the request body, and an actor
        of any other class (a session, a channel, a terminal, a Lead, an agent) is
        refused here — before the wire, so the Manager's own class refusal is the
        second wall and not the first. No new role exists: the operator is a host
        operator, named by the identity its WG session already proved.

        What comes back is 202 and best-effort: the replacement kills the process
        that made this call, so this client will not be alive to see a verdict
        (H-6). A `succeeded` here would be a lie told by a dead process.
        """
        if actor.name != OPERATOR_ACTOR:
            return HopResult(ok=False, said=(f"the actor is not a {OPERATOR_ACTOR} (caught {actor.name!r}); "
                                             "the operator is derived from the authenticated host-WG "
                                             f"session ({OPERATOR_SOURCE!r}), never supplied by the page"))
        body: dict[str, Any] = {"reason": reason, "actor": actor.name, "actor_source": actor.source}
        if action_id:
            body["action_id"] = action_id
        return self._call("POST", "/api/v1/office/ui/restart", body)


def operator_from_session(source: str) -> Actor:
    """Derive the operator from the authenticated session, or refuse.

    One place, so the surface cannot grow a second idea of who the operator is.
    The actor is the class token the ruling names — `host_operator` — not the
    person's name: the person is proved at the door that admitted the session, and
    this Manager cannot re-derive them (the ruling says so plainly). A session
    from any other door raises rather than quietly producing a token the Manager
    will refuse.
    """
    if source != OPERATOR_SOURCE:
        raise ValueError(f"a {source!r} session is not a host-WG session; it cannot act as the operator")
    return Actor(name=OPERATOR_ACTOR, source=OPERATOR_SOURCE)
