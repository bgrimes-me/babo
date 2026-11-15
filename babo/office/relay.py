"""The office relay: the pod's only window, and the five calls it answers.

``docs/OFFICE-RELAY.md`` is the contract of record. ``hop.py`` is the pattern of
record: the agent's local door arrives as an **injected transport**, so every row
in ``tests/baton/test_office_relay.py`` runs on a runner with no container. The
signature is hop's, on purpose and by copy rather than by import — the two live in
different pods, and neither should depend on the other::

    transport(method, url, body, timeout_s) -> (status, body as text)

Five rules, each of which is a row:

- **It reports. It does not decide.** Every answer the relay cannot substantiate
  is ``{"unknown": "<call>: <reason>"}``, and the success fields are *absent* from
  that payload — never ``false``. ``seen: false`` is a claim a blind relay cannot
  make; ``delivered: false`` is a claim about an effect nobody observed.
- **The status code is a claim about this hop.** These handlers choose no status
  code at all; the HTTP glue answers ``200`` even for an agent that never replied,
  because the relay is fine and the agent is not answering. The absence of the
  success fields is the fact.
- **A refusal is an answer; ``unknown`` is not.** A 4xx carrying a sentence in
  ``detail`` is the agent's own words and passes through untouched. ``404`` and
  ``405`` are not refusals — they are the absence of a door, and the relay says so
  rather than reporting a router artefact as the agent's opinion.
- **``health`` makes no claim about the agent**, and ``status`` never turns
  silence into ``in_flight: false``.
- **``message`` reports what the door it used can confirm, and nothing else.**
  The one door behind this call today (``babo/api/server.py``'s
  ``POST /v1/chat/completions``) does not accept a message and return; it takes a
  whole turn — the server awaits the turn before it answers — and then returns.
  So a 2xx here means **a turn was taken and returned**, not that an entry was
  queued, and two messages to one caller serialize on that session lock. That is
  why a timeout is ``unknown`` and says so: the relay cannot tell "never
  arrived" from "still working", and inventing either is the failure this line
  exists to prevent. ``/v1/office/message`` — accept, queue, return, own the
  "accepted" claim — is item 2's fourth route, and the day it lands is the day
  ``delivered`` means delivery rather than completion.

Only ``message`` has a route to knock on today. ``status``, ``quiesce`` and
``flush`` name the paths they will ask, once, here: the relay is the only
component that will ever ask them, so the paths are declared in one place and the
agent grows the routes behind them — **importing these constants, never
re-typing them.** A path that drifts by one prefix leaves the relay saying "the
door has no /v1/office/status" forever while every row here stays green, which is
the green that means nothing. Until the routes land, the honest answer is
``unknown`` — and a row that expects ``unknown`` from those three is a **pass**,
not a red.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, cast

# The wire version from the contract, not the package version: a consumer parses
# this number, and it changes when the shapes change.
RELAY_VERSION = "1"

# The one door that exists today (babo/api/server.py:556). The other three are the
# routes item 2 adds behind them; named here so that nobody has to derive them a
# second time in the server.
CHAT_COMPLETIONS = "/v1/chat/completions"
AGENT_STATUS = "/v1/office/status"
AGENT_QUIESCE = "/v1/office/quiesce"
AGENT_FLUSH = "/v1/office/flush"

# The fields of a substantiated position. Copied when the agent sends them,
# omitted when it does not — never defaulted, because a default here is a claim.
# Plural on purpose: the agent holds one lock per session, so a pod can be running
# several turns at once, and a singular position lets a Manager restart into a
# live one.
POSITION_FIELDS = ("in_flight", "count", "turns")
TURN_FIELDS = ("session", "turn_id", "wall_started_at")

Transport = Callable[[str, str, dict[str, Any] | None, float], tuple[int, str]]


def relay_transport(method: str, url: str, body: dict[str, Any] | None, timeout_s: float) -> tuple[int, str]:
    """The default door transport: one request, one timeout, no retry.

    A retry that turns "no answer" into "delivered" is the one thing the contract
    forbids outright, so there is no ladder here and no second attempt.
    """
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace") if exc.fp else ""


def _unknown(call: str, reason: str) -> dict[str, Any]:
    """The one failure shape. One key, always a string, always prefixed."""
    return {"unknown": f"{call}: {reason}"}


def _json_object(text: str) -> dict[str, Any] | None:
    if not text.strip():
        return None
    try:
        loaded: object = json.loads(text)
    except ValueError:
        return None
    return cast(dict[str, Any], loaded) if isinstance(loaded, dict) else None


def _sentence(payload: dict[str, Any] | None) -> str:
    """The agent's own sentence, if it left one, verbatim and trimmed."""
    if payload is None:
        return ""
    said = payload.get("detail")
    return said.strip() if isinstance(said, str) else ""


def _no_answer(exc: BaseException, timeout_s: float, timeout_note: str = "") -> str:
    """Why the door did not answer, in words a caller can act on.

    A timeout is named as a timeout because it is the one failure whose reason is
    the relay's own deadline rather than anything the agent said or did. A call
    that must be more specific about what the deadline leaves unknown — ``message``,
    where a turn may still be running — passes the sentence to add.
    """
    if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError):
        if timeout_note:
            return f"the agent did not answer its local door within {timeout_s}s, {timeout_note}"
        return f"the agent did not answer its local door (timeout after {timeout_s}s)"
    said = str(exc).strip() or type(exc).__name__
    return f"the agent did not answer its local door ({said})"


def _turns_named(named: list[object]) -> list[dict[str, Any]]:
    """The turns the agent named, narrowed in one place.

    Every caller has already established that this is a list of objects — that is
    part of what ``_says_turn_position`` substantiates — so the filter here is the
    narrowing, not a second policy.
    """
    turns: list[dict[str, Any]] = []
    for turn in named:
        if isinstance(turn, dict):
            turns.append(cast(dict[str, Any], turn))
    return turns


def _position(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy the position out of an answer, and nothing else.

    The surface is closed: a field the relay has never heard of is not forwarded,
    because a consumer parses five calls with one shape and an unknown key is how
    "could not see" starts to look like "fine, and by the way". The same rule runs
    inside each turn — ``session``, ``turn_id`` and ``wall_started_at`` travel;
    anything else the agent attaches to a turn does not.
    """
    out: dict[str, Any] = {name: payload[name] for name in POSITION_FIELDS if name in payload}
    if "turns" in out:
        out["turns"] = [
            {name: turn[name] for name in TURN_FIELDS if name in turn}
            for turn in _turns_named(cast(list[object], payload["turns"]))
        ]
    return out


def _named(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _says_turn_position(payload: dict[str, Any]) -> str | None:
    """A position is one claim, and half of one is not an answer.

    Five ways to be half a position, all answered ``unknown``: no boolean
    ``in_flight``; no ``turns`` list; a list that contradicts ``in_flight`` in
    either direction; a ``count`` that disagrees with the list; and a turn that
    cannot say which session it is or what it is called. Forwarding any of them
    hands the caller a position they can neither use nor check — the same failure
    as filling a success field with ``false``, one layer in.
    """
    in_flight = payload.get("in_flight")
    if not isinstance(in_flight, bool):
        return "the agent answered without saying whether a turn is in flight"
    named = payload.get("turns")
    if not isinstance(named, list):
        return "the agent answered without saying which turns are in flight"
    for raw in cast(list[object], named):
        if not isinstance(raw, dict):
            return "the agent named a turn that is not an object"
    turns = _turns_named(cast(list[object], named))
    if in_flight and not turns:
        return "the agent says a turn is in flight and named none"
    if not in_flight and turns:
        return "the agent named a turn in flight and says nothing is running"
    count = payload.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count != len(turns):
        return "the agent's count disagrees with the turns it named"
    for turn in turns:
        if not _named(turn.get("session")):
            return "the agent named a turn in flight and did not say which session it is"
        if not _named(turn.get("turn_id")):
            return "the agent named a turn in flight and did not name it"
    return None


def _accepted_turn_position(payload: dict[str, Any]) -> str | None:
    if payload.get("accepted") is False:
        return "the agent answered without accepting the quiesce"
    return _says_turn_position(payload)


def _store_written(payload: dict[str, Any]) -> str | None:
    if payload.get("persisted") is not True:
        return "the agent answered without confirming the store was written"
    return None


@dataclass
class Relay:
    """Five calls into one pod, over one injected transport.

    ``door_url`` is the agent's local door — loopback in every real deployment,
    because the agent binds loopback and never the wire. Nothing outside the pod
    talks to the agent; this object is the only thing that does.
    """

    door_url: str
    transport: Transport = relay_transport
    status_timeout_s: float = 2.0
    quiesce_timeout_s: float = 5.0
    flush_timeout_s: float = 5.0
    message_timeout_s: float = 30.0
    clock: Callable[[], float] = time.monotonic
    started_at: float = field(init=False, repr=False, default=0.0)

    def __post_init__(self) -> None:
        self.started_at = self.clock()

    # -- the surface ------------------------------------------------------
    def health(self) -> dict[str, Any]:
        """Is the relay up. It asks nobody, and therefore says nothing about them."""
        uptime = max(0.0, self.clock() - self.started_at)
        return {"ok": True, "relay": {"version": RELAY_VERSION, "uptime_s": int(uptime)}}

    def status(self) -> dict[str, Any]:
        """Where the agent's turns are, asked locally. Silence is not ``idle``.

        Plural on purpose: several sessions can be running in one pod, and a
        restart decided against one of them runs into the others.
        """
        payload, failure = self._ask(
            "status", "GET", AGENT_STATUS, None, self.status_timeout_s, _says_turn_position,
            allow_sentence=True,
        )
        if failure is not None:
            return failure
        assert payload is not None
        return {"seen": True, **_position(payload)}

    def quiesce(self, reason: str) -> dict[str, Any]:
        """Stop admitting work and say where the turns stand. Quiesce, not kill.

        The position it returns is ``status``'s shape, for the same reason: an
        admission gate that can only name one of two live turns has closed nothing.
        """
        payload, failure = self._ask(
            "quiesce", "POST", AGENT_QUIESCE, {"reason": reason}, self.quiesce_timeout_s,
            _accepted_turn_position, allow_sentence=True,
        )
        if failure is not None:
            return failure
        assert payload is not None
        return {"accepted": True, **_position(payload)}

    def flush(self, reason: str) -> dict[str, Any]:
        """Persist and confirm. It never claims a persist it cannot substantiate."""
        payload, failure = self._ask(
            "flush", "POST", AGENT_FLUSH, {"reason": reason}, self.flush_timeout_s,
            _store_written, allow_sentence=True,
        )
        if failure is not None:
            return failure
        assert payload is not None
        confirmed = _sentence(payload)
        return {"persisted": True, **({"detail": confirmed} if confirmed else {})}

    def message(self, *, source: str, channel: str, content: str,
                attachments: tuple[str, ...] = ()) -> dict[str, Any]:
        """Deliver one message. ``delivered: true`` only on a 2xx that came back.

        **What that 2xx means is decided here, by the door chosen below.** The
        door today is ``/v1/chat/completions``, which takes a turn: the server
        awaits it before answering (``babo/api/server.py``), so a 2xx means *the
        agent took a turn and returned*, not *an entry was queued*, and two
        messages to one caller serialize on that session's lock. ``delivered``
        is therefore the strongest word this door can support, and the weakest
        word a notification needs. ``/v1/office/message`` (item 2) is the route
        where 2xx means accepted-for-delivery; when it lands, this call changes
        one constant and the word gets its ordinary meaning.

        ``from`` and ``channel`` are not decoration: they become the door's
        session identity, because a caller that cannot say who is speaking cannot
        be answered in the right conversation. Attachments are refused by name
        rather than dropped — a rebuild that forgets a field is how identity was
        deleted from the store once already.
        """
        if attachments:
            return _unknown("message", "the agent's local door does not accept attachments yet")
        body: dict[str, Any] = {
            "messages": [{"role": "user", "content": content}],
            "session_id": f"{channel}:{source}",
        }
        # Deliberately no "model": the door compares it against its configured
        # model and 400s on a mismatch, and the relay has no business choosing.
        _, failure = self._ask(
            "message", "POST", CHAT_COMPLETIONS, body, self.message_timeout_s,
            None, allow_sentence=False, parse=False,
            timeout_note="so whether it took the turn is unknown",
        )
        if failure is not None:
            return failure
        return {"delivered": True}

    # -- one door, one shape ----------------------------------------------
    def _ask(self, call: str, method: str, path: str, body: dict[str, Any] | None,
             timeout_s: float, substantiate: Callable[[dict[str, Any]], str | None] | None,
             *, allow_sentence: bool, parse: bool = True,
             timeout_note: str = "") -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Ask the agent's door once.

        Returns ``(payload, None)`` on an answer and ``(None, body)`` on anything
        else — where ``body`` is either the ``unknown`` shape or the agent's own
        ``detail`` sentence. Never raises: a transport is outside our control.
        """
        url = self.door_url.rstrip("/") + path
        try:
            status, text = self.transport(method, url, body, timeout_s)
        except Exception as exc:  # report, never raise: the door is not ours
            return None, _unknown(call, _no_answer(exc, timeout_s, timeout_note))

        # The body is parsed even when a call does not need it, because a 4xx's
        # sentence is the agent's own words and losing it would leave the caller
        # with a number where an answer had been.
        payload = _json_object(text)
        if 200 <= status < 300:
            if not parse:
                return {}, None
            if payload is None:
                return None, _unknown(call, f"the agent answered {status} with a body that is not a JSON object")
            if substantiate is not None:
                reason = substantiate(payload)
                if reason is not None:
                    return None, _unknown(call, reason)
            return payload, None

        said = _sentence(payload)
        if status in (404, 405):
            # Not a refusal: there is no route there. Until item 2 lands, this is
            # what the three serviceless calls answer, and it is the truth.
            tail = f": {said}" if said else ""
            return None, _unknown(call, f"the agent's door has no {path} (HTTP {status}{tail})")
        if allow_sentence and 400 <= status < 500 and said:
            return None, {"detail": said}
        quoted = f": {said}" if said else ""
        return None, _unknown(call, f"the agent's local door did not accept it (HTTP {status}{quoted})")
