"""The Manager's side of the hop: handlers, validation, and the peer check.

D-12 makes the UI the only caller: as ruled, the UI pod is 10.0.0.11 and the
Manager (this service) is 10.0.0.12. Neither number is a default here — the peer
list is a required argument, so the render owns the values and this module only
compares what it is given. That is enforced twice on purpose — the
Router admits only the UI pod's address to this port, and these handlers compare
the **socket peer** to that same exact address, header-blind. A wrong network
rule therefore still gets a 403 from the application rather than an open door.

The handlers are pure: they take a peer string and a parsed body, and return
``(status, payload)``. The listener, its binding and its policy belong to the
network lane; keeping the interpretation here means the rules can be tested
without a socket.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from babo.office.api import (
    AGENT_ACTIONS,
    OPERATOR_ACTIONS,
    OPERATOR_ACTOR,
    TERMINAL,
    OfficeManager,
)


def _named(exc: BaseException, *names: str) -> bool:
    """Match a core exception by class name.

    The core's exception types live in ``babo/office/core.py``, which is another
    branch's file until the slice lands; importing them here would make this
    surface unimportable on its own head. The name match is deliberately narrow —
    anything that is not one of these keeps propagating, because a swallowed
    programming error is worse than a loud 500.
    """
    return type(exc).__name__ in names

# The handle a restart-ui action carries: the UI *pod's* name, because the audit
# row must name something an operator can look up, and the core's dispatch does a
# real container lookup. A synthetic "ui" would be neither an agent handle nor a
# pod name — an illegible row and an impossible lookup (soro's H-5).
UI_POD = "babo-ui"

# A body may carry these and nothing else; unknown keys are refused rather than
# ignored, because an ignored key is a client believing it changed something.
ACTION_FIELDS = frozenset({"handle", "action", "reason", "actor", "actor_source", "action_id"})


def peer_allowed(peer: str, allowed: tuple[str, ...]) -> bool:
    """Exact address match, no prefixes and no header trust — the trusted_sources rule."""
    return any(entry == peer for entry in allowed)


@dataclass
class ManagerService:
    core: OfficeManager
    ui_peers: tuple[str, ...]
    # The host-WG door's admitted addresses — the only callers `restart-ui`
    # answers. Both sets are required arguments and neither has a default: the
    # render owns the addresses, and an unset door is a closed door.
    operator_peers: tuple[str, ...]

    # -- reads -----------------------------------------------------------
    def health(self) -> tuple[int, dict[str, Any]]:
        available, detail = self.core.health()
        if available:
            return 200, {"available": True, "detail": detail, "core": getattr(self.core, "name", "core")}
        return 503, {"available": False, "detail": detail}

    def agents(self, peer: str) -> tuple[int, dict[str, Any]]:
        if not peer_allowed(peer, self.ui_peers):
            return 403, {"detail": f"{peer} is not the UI pod"}
        return 200, {"agents": [a.overview() for a in self.core.agents()]}

    # -- actions ---------------------------------------------------------
    def act(self, peer: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not peer_allowed(peer, self.ui_peers):
            return 403, {"detail": f"{peer} is not the UI pod"}
        unknown = set(body) - ACTION_FIELDS
        if unknown:
            return 400, {"detail": f"unknown field(s): {', '.join(sorted(unknown))}"}
        handle = str(body.get("handle") or "").strip()
        action = str(body.get("action") or "").strip()
        reason = str(body.get("reason") or "").strip()
        actor = str(body.get("actor") or "").strip()
        actor_source = str(body.get("actor_source") or "").strip()
        replay = body.get("action_id")
        if not handle:
            return 400, {"detail": "missing handle"}
        if action in OPERATOR_ACTIONS:
            # The agent path does not carry the pod verb. This is a refusal by
            # name, not an unknown word: the verb exists, the door does not.
            return 403, {"detail": (f"{action} is the host operator's verb, admitted at the host-WG "
                                    "door; it is not carried on the agent path")}
        if action not in AGENT_ACTIONS:
            return 400, {"detail": f"unknown action {action!r}; it is one of {', '.join(AGENT_ACTIONS)}"}
        if not reason:
            return 400, {"detail": "missing reason"}
        if not actor or not actor_source:
            # R5: no actor, no effect. Refuse here rather than let the core do it,
            # because this surface is the one that must not accept an anonymous call.
            return 400, {"detail": "missing actor or actor_source; an action must name who is asking"}
        try:
            action_id = self.core.act(handle, action, reason, actor=actor, actor_source=actor_source,
                                      action_id=str(replay) if isinstance(replay, str) and replay else None)
        except Exception as exc:
            if not _named(exc, "CoreRefusalError", "RefusalError"):
                raise
            # A refusal is an answer, not a server fault: nothing was queued, so
            # this is 409 carrying the core's own sentence.
            return 409, {"detail": str(exc)}
        return 202, {"action_id": action_id}

    def restart_ui(self, peer: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """The cross-pod half of revocation: replace the UI pod.

        It is an action, not a side door: same actor/reason requirement, same
        idempotency key, same closed terminal states. The ledger records that it
        invalidates sessions by construction rather than by an API call reaching
        into another pod's memory.

        Two admissions, one principal (ruled 2026-11-11). The API-source check
        guards who may reach the port; the authority check guards whose act it is,
        and it is an allow-list of exactly one: the row must carry both
        `handle == "babo-ui"` and `actor == "host_operator"`. Everything else — a
        Lead, a crew agent, a service identity, a session, an unauthenticated local
        process — is refused here **before effect**, with the class it caught named.

        The limit is the ruling's own: this Manager cannot re-derive the operator,
        so it checks the class token the caller carried. This verb's authority
        rests on the door that authenticated the operator, and the refusal catches a
        wrong class, not a forged one.

        The answer is 202 and it is **best-effort** (Bomu, H-6): replacing the pod
        kills the process that asked, so a client that died mid-request cannot be
        shown a verdict, and no client ever sees this action reach `succeeded` from
        inside that process. The action row is the witness, not the response.
        """
        if not (peer_allowed(peer, self.ui_peers) or peer_allowed(peer, self.operator_peers)):
            return 403, {"detail": f"{peer} is not an admitted door for {OPERATOR_ACTIONS[0]}"}
        unknown = set(body) - {"reason", "actor", "actor_source", "action_id"}
        if unknown:
            return 400, {"detail": f"unknown field(s): {', '.join(sorted(unknown))}"}
        reason = str(body.get("reason") or "").strip()
        actor = str(body.get("actor") or "").strip()
        actor_source = str(body.get("actor_source") or "").strip()
        replay = body.get("action_id")
        if not reason or not actor or not actor_source:
            return 400, {"detail": "missing reason, actor or actor_source"}
        if actor != OPERATOR_ACTOR:
            # The allow-list of exactly one, and the refusal names the class it
            # caught (soro's §8). `actor_source` says which door the claim came
            # through; it is not the authority, and neither is the peer address.
            return 403, {"detail": (f"the actor is not a {OPERATOR_ACTOR} (caught {actor!r}); "
                                    f"{OPERATOR_ACTIONS[0]} carries exactly one class")}
        try:
            action_id = self.core.act(UI_POD, "restart-ui", reason, actor=actor, actor_source=actor_source,
                                      action_id=str(replay) if isinstance(replay, str) and replay else None)
        except Exception as exc:
            if not _named(exc, "CoreRefusalError", "RefusalError"):
                raise
            return 409, {"detail": str(exc)}
        return 202, {
            "action_id": action_id,
            "status": "accepted",
            "best_effort": True,
            "note": ("accepted, best-effort: this action replaces the pod, so the caller that would "
                     "have polled will not see a terminal state — no client sees `succeeded`. "
                     "Read the action row for the verdict."),
        }

    def action(self, peer: str, action_id: str) -> tuple[int, dict[str, Any]]:
        if not peer_allowed(peer, self.ui_peers):
            return 403, {"detail": f"{peer} is not the UI pod"}
        try:
            record = self.core.action(action_id)
        except Exception as exc:
            if not _named(exc, "UnknownActionError", "KeyError"):
                raise
            # An unknown id is 404, never an invented `failed`.
            return 404, {"detail": str(exc)}
        if record.state not in ("pending", "running", *TERMINAL):
            # A core bug must not reach the UI as a fifth word.
            return 503, {"detail": f"the core reported an unknown action state {record.state!r}"}
        return 200, {"state": record.state, "detail": record.said}
