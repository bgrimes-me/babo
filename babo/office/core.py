"""The native manager core: deterministic lifecycle, refusals, records.

The frozen ledger is the specification; this module is the first slice of it
(lifecycle + credential ledger). What is here, and what is deliberately not:

- **Decisions are pure and deterministic** (A-04). ``decide`` takes the verb, the
  runtime's facts and the record and returns a refusal detail, a permit, or a
  *resolution*: the verb is already satisfied by the world, so there is nothing
  to execute (L-07's word is "resolves"). The kind travels in the value, not
  only in the table, so the executor can see the same fact the table saw. No
  model, no clock-dependent branch outside the deadline, no I/O.
- **Refusals precede effects** (L-03, U-03): every precondition is checked before
  a pod is touched, and the refusal detail names the handle and the state that
  made it impossible.
- **The runtime's word is the state word** (O-01, U-04). Observed states are
  read from the runtime and transported with their provenance; a disagreement is
  ``unknown`` with a note, never resolved by preference.
- **Records are durable** (R-03, R-08). The action row and its audit row are
  written in the same transaction; the in-memory view is not the record.
- **Nothing here opens the podman socket.** ``runtime.py`` does, and under D-10
  it lives in ``babo-core``. No runtime handle crosses the Office interface.

Not in this slice, named rather than hidden (see the PR's gap list): the pod
renderer (``spec_for``), the UI pod renderer (``ui_spec``) and the agent
stand-down handshake (``agent_control``) are injected ports with no production
implementation yet; the mesh peer freeze in destroy and memory-pressure policy
(L-09) need ``network.py`` and a settings value respectively.

L-10 (``restart-ui``) is the recorded sixth verb: it replaces the UI pod, and
thereby kills the sessions the old process held in memory. It acts on a pod, not
an agent, so no agent identity is consulted. The sentence that keeps D-12
intact is Bomu's: **UI-initiated, Manager-executed, creates no channel, and the
Manager still initiates nothing on its own.**
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from babo.office import model
from babo.office.api import ACTIONS, TERMINAL, ActionRecord
from babo.office.audit import ActionRow, Store
from babo.office.credentials import CredentialLedger
from babo.office.runtime import (
    AgentControl,
    ContainerFacts,
    ControlUnavailableError,
    LaunchSpec,
    PodmanRuntime,
    Runtime,
    RuntimeUnavailableError,
    UnwiredAgentControl,
)

# Labels a rendered agent pod carries; the runtime is searched by these.
MANAGED_LABEL = "babo.managed"
HANDLE_LABEL = "babo.handle"
# The UI pod's name, one spelling. Its action row's handle is this name, not a
# synthetic agent handle (L-10). If `api.py` ever carries the constant, import it
# rather than restating it.
UI_POD = "babo-ui"
# L-01's closed set plus L-10's recorded amendment. `ACTIONS` is the surface's
# word list and `api.py` is the interface owner's file; the core accepts the
# sixth verb from the day the amendment is ruled, and this is an *ordered union*
# rather than a concatenation so that no future day can name the verb twice —
# once the surface carries it, the tuple is the surface's own.
CORE_ACTIONS: tuple[str, ...] = tuple(dict.fromkeys((*ACTIONS, "restart-ui")))
# Automation is a first-class actor, recorded verbatim (soro, R5 note).
SYSTEM_ACTORS = ("system:watchdog", "system:reconcile", "system:boot")

POLL_S = 0.5


class CoreRefusalError(Exception):
    """The boundary declined and nothing was written (L-01: unknown verb)."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class UnknownActionError(CoreRefusalError):
    """No such action id — not-found, never a synthesised `failed` (L-04)."""


@dataclass(frozen=True)
class Identity:
    identity: str
    channel: str


@dataclass(frozen=True)
class Refusal:
    detail: str


@dataclass(frozen=True)
class Permit:
    pass


@dataclass(frozen=True)
class Resolved:
    """The verb is already satisfied; the executor must not act (L-07)."""

    detail: str


Decision = Refusal | Permit | Resolved


def decide(verb: str, facts: ContainerFacts | None, identity: Identity | None,
           handle: str, record_state: str = "") -> Decision:
    """The precondition table (L-03), pure: no I/O, no clock, no model.

    ``record_state`` is the core's own launch record (`alive`, `starting`, …).
    It is an input to L-07's spawn short-circuit: a spawn resolves to the
    existing pod for an alive record and to `starting: true` for a starting one,
    so it never reaches the runtime a second time.
    """
    if verb in ("spawn", "wake"):
        # A pod that is already there resolves the verb before identity is even
        # consulted (office_action_runner.rb:56 for wake; spawner.rb:18-25 for
        # spawn's alive/starting short-circuit). The detail is the verdict, so a
        # caller that reaches the executor by another road still learns that
        # nothing needed running.
        if facts is not None and facts.alive:
            if verb == "wake":
                return Resolved(f"{handle} is already alive; nothing to wake")
            return Resolved(f"{handle} is already alive; nothing to spawn (existing pod)")
        if verb == "spawn" and record_state == "starting":
            return Resolved(f"{handle} is starting; nothing to spawn (starting: true)")
        if verb == "spawn" and record_state == "alive":
            return Resolved(f"{handle} has an alive launch record; nothing to spawn (existing pod)")
        if identity is None:
            return Refusal(f"no identity/channel recorded for {handle}; cannot {verb}")
        return Permit()
    if verb == "restart-ui":
        # L-10. The precondition is the pod, not an agent: a restart of nothing
        # is not a success, and a pod that is not alive is a spawn question, not
        # a restart one (its sessions already died with it). The caller's
        # authority is the service's gate (A-03); no identity is consulted here
        # because the UI pod has none. UI-initiated, Manager-executed, creates no
        # channel, and the Manager still initiates nothing on its own.
        if facts is None:
            return Refusal(f"nothing to restart: no {UI_POD} pod")
        if not facts.alive:
            return Refusal(f"nothing to restart: {UI_POD} is {facts.state}, not alive")
        return Permit()
    if verb == "freeze":
        if facts is None:
            return Refusal(f"nothing to freeze: no pod for {handle}")
        if not facts.alive:
            return Refusal(f"{handle} is {facts.state}, not alive; nothing to freeze")
        return Permit()
    if verb == "rescue":
        if facts is None:
            return Refusal(f"nothing to rescue: no pod for {handle}")
        return Permit()
    if verb == "destroy":
        if facts is None:
            return Refusal(f"nothing to destroy: no pod for {handle}")
        return Permit()
    return Refusal(f"unknown action {verb!r}; it is one of {', '.join(ACTIONS)}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(stamp: str) -> datetime:
    parsed = datetime.fromisoformat(stamp)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True, name="babo-office-action").start()


class NativeManager:
    """The Office interface's implementation (``api.OfficeManager``)."""

    name = "babo-core"

    def __init__(
        self,
        *,
        store: Store,
        runtime: Runtime,
        ledger: CredentialLedger,
        clock: Callable[[], str] = now_iso,
        boot_id: str = "",
        deadline: float = 120.0,
        executor: Callable[[Callable[[], None]], None] | None = None,
        identity_of: Callable[[str], Identity | None] | None = None,
        spec_for: Callable[[str, Identity], LaunchSpec] | None = None,
        ui_spec: Callable[[], LaunchSpec] | None = None,
        agent_control: AgentControl | None = None,
        mesh_freeze: Callable[[str], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.ledger = ledger
        self.clock = clock
        self.boot_id = boot_id or os.environ.get("BABO_BOOT_ID") or f"{int(time.time())}"
        self.deadline = float(deadline)
        self._executor = executor or _thread
        self._identity_of = identity_of
        self._spec_for = spec_for
        self._ui_spec = ui_spec
        self._agent_control = agent_control or UnwiredAgentControl()
        self._mesh_freeze = mesh_freeze
        self._sleep = sleep

    # -- reads ----------------------------------------------------------
    def health(self) -> tuple[bool, str]:
        try:
            self.runtime.inspect_container("__babo-core-probe__")
        except RuntimeUnavailableError as exc:
            return False, str(exc)
        return True, "native core; runtime reachable"

    def agents(self) -> list[model.AgentObserved]:
        try:
            facts = self.runtime.list_agents()
            error = ""
        except RuntimeUnavailableError as exc:
            facts = []
            error = str(exc)

        launches = {row["handle"]: row for row in self.store.launches()}
        observed: dict[str, model.AgentObserved] = {}

        if error:
            for handle in sorted(launches):
                observed[handle] = model.AgentObserved(
                    handle=handle, state="unknown",
                    provenance={"podman": "error", "record": "present",
                                "note": f"runtime could not be read: {error}"},
                )
            return list(observed.values())

        for found in facts:
            handle = found.labels.get(HANDLE_LABEL) or found.name
            if handle == UI_POD or found.name == UI_POD:
                # §8.1: the UI pod has a lifecycle, not a workspace; an agent view
                # that lists its own front door is a phantom row.
                continue
            observed[handle] = model.AgentObserved(
                handle=handle,
                state=_observed_state(found),
                wg_address=found.ip,
                last_activity_at=found.started_at,
                checkouts=self._checkouts(handle),
                provenance=_provenance(found),
            )

        # Absent pods are still answers (M5). A destroyed agent is omitted on
        # purpose so join() renders missing, not dead (D-2/M7).
        for handle, launch in sorted(launches.items()):
            if handle == UI_POD:
                continue  # §8.1, same filter on the record side
            if handle in observed:
                continue
            state = launch.get("state") or ""
            ending = launch.get("ending") or ""
            if state == "inactive" or ending == "destroyed":
                continue
            # F-1 (corrected): decide by the recorded state first; the ending is
            # only a fallback for rows written before the core wrote its own reason.
            if state == "frozen":
                reason = ending if ending and ending != "idle" else "standdown"
                note = ("no pod; the core released it after a stand-down request "
                        "(work preserved)")
                if ending == "idle":
                    note += "; a pre-fix record wrote the session processor's word 'idle' here"
                observed[handle] = model.AgentObserved(
                    handle=handle, state="frozen",
                    checkouts=self._checkouts(handle),
                    provenance={"podman": "absent", "record": state,
                                "ended_reason": reason, "note": note},
                )
            elif ending in ("idle", "context_limit"):
                # A pre-fix row with no frozen state: read it, do not migrate it.
                observed[handle] = model.AgentObserved(
                    handle=handle, state="frozen",
                    checkouts=self._checkouts(handle),
                    provenance={"podman": "absent", "record": state, "ended_reason": ending,
                                "note": "no pod; the last session ended gracefully "
                                        "(recorded before the core wrote its own reason)"},
                )
            elif state == "rescued":
                observed[handle] = model.AgentObserved(
                    handle=handle, state="unknown",
                    checkouts=self._checkouts(handle),
                    provenance={"podman": "absent", "record": state,
                                "note": "no pod; last action was rescue (workspace preserved)"},
                )
            else:
                observed[handle] = model.AgentObserved(
                    handle=handle, state="dead",
                    checkouts=self._checkouts(handle),
                    provenance={"podman": "absent", "record": state,
                                "note": "no pod; no graceful ending recorded"},
                )
        return sorted(observed.values(), key=lambda agent: agent.handle)

    def action(self, action_id: str) -> ActionRecord:
        row = self.store.get_action(action_id)
        if row is None:
            raise UnknownActionError(f"no such action: {action_id}")
        return ActionRecord(id=row.id, handle=row.handle, action=row.verb, state=row.state,
                           said=row.said)

    # -- the one write boundary -----------------------------------------
    def act(self, handle: str, action: str, reason: str, *, actor: str = "",
            actor_source: str = "", action_id: str | None = None) -> str:
        if action not in CORE_ACTIONS:
            # L-01: refused before a row exists or a thread starts. The list is
            # CORE_ACTIONS so the sentence below can never name a verb as both
            # known and unknown (Bomu's L-10 acceptance (f)).
            raise CoreRefusalError(
                f"unknown action {action!r}; it is one of {', '.join(CORE_ACTIONS)}"
            )
        if not handle or not handle.strip():
            raise CoreRefusalError("no handle recorded; nothing to act on")

        # Replay of a known row: no new row, no dispatch, no effect (L-07).
        if action_id:
            existing = self.store.get_action(action_id)
            if existing is not None:
                if existing.handle != handle or existing.verb != action:
                    raise CoreRefusalError(
                        f"action id {action_id} belongs to ({existing.handle}, {existing.verb}), "
                        f"not ({handle}, {action})"
                    )
                return existing.id

        # A-03 / R5: the actor and the reason are the approval record. Their
        # absence is a refusal, written down (U-03), not a warning.
        if not actor or not actor.strip():
            return self._refuse(handle, action, actor="<none>", actor_source="", reason=reason,
                                detail=f"no approval recorded for {handle} (asked by <none>)")
        if not actor_source or not actor_source.strip():
            # F-7 as a gate: a name without provenance is a string that satisfied
            # a check, not an identity. Its own detail, not the missing-actor one.
            return self._refuse(handle, action, actor=actor, actor_source="", reason=reason,
                                detail=f"no actor source recorded for {handle} (asked by {actor}); "
                                       "a name without provenance is not an identity")
        if not reason or not reason.strip():
            return self._refuse(handle, action, actor=actor, actor_source=actor_source, reason="",
                                detail=f"no reason recorded for {handle} ({_asked(actor, actor_source)})")

        identity = self._identity_of(handle) if self._identity_of else None
        launch = self.store.launch(handle) or {}
        record_state = str(launch.get("state") or "")
        facts: ContainerFacts | None = None
        if action in ("spawn", "wake", "freeze", "rescue", "destroy", "restart-ui"):
            try:
                facts = self.runtime.inspect_container(handle)
            except RuntimeUnavailableError as exc:
                # Cannot decide without facts; refuse rather than assume absence.
                return self._refuse(handle, action, actor=actor, actor_source=actor_source,
                                    reason=reason,
                                    detail=f"runtime could not be read, so {action} cannot be "
                                           f"decided for {handle}: {exc}",
                                    identity=identity.identity if identity else "")

        verdict = decide(action, facts, identity, handle, record_state)
        if isinstance(verdict, Refusal):
            return self._refuse(handle, action, actor=actor, actor_source=actor_source,
                                reason=reason, detail=verdict.detail,
                                identity=identity.identity if identity else "")

        # Immediate no-ops are honest successes, in the table's own words (L-07).
        # The rule lives in `decide`, not in a positional early return here, so an
        # executor reached by another road cannot miss it.
        if isinstance(verdict, Resolved):
            return self._settle(handle, action, actor, actor_source, reason, "succeeded",
                                verdict.detail, identity=identity.identity if identity else "")

        created = self.clock()
        new_id = action_id or secrets.token_hex(12)
        row = self.store.create_action(action_id=new_id, handle=handle, verb=action,
                                       actor=actor, actor_source=actor_source, reason=reason,
                                       boot_id=self.boot_id, at=created)
        claimed = self.store.claim_or_read(
            handle, action, new_id, self._expires_at(created), created
        )
        if claimed != new_id:
            # L-07: a duplicate is refused, and the detail names the in-flight
            # action id. The refused row is returned so the refusal is visible
            # at the surface, not only in the store (U-03).
            self.store.finish_action(
                row.id, state="refused", at=self.clock(),
                said=f"action {claimed} is already pending for {handle} ({action}); not accepted",
                event="REFUSED", identity=identity.identity if identity else "",
            )
            return row.id
        self._executor(lambda: self._run(row.id))
        return row.id

    # -- action execution ----------------------------------------------
    def _run(self, action_id: str) -> None:
        row = self.store.get_action(action_id)
        if row is None or row.terminal:
            return
        begun = self.clock()
        if _parse(begun) > _parse(self._expires_at(row.created_at)):
            self._finish(row, "timeout", "no verdict before the deadline; outcome unknown",
                         event=_event(row.verb, "timeout"))
            return
        if not self.store.start_action(action_id, begun):
            return
        identity = self._identity_of(row.handle) if self._identity_of else None
        try:
            state, said, event = self._execute(row, identity)
        except ControlUnavailableError as exc:
            state, said, event = "failed", str(exc), _event(row.verb, "failed")
        except RuntimeUnavailableError as exc:
            state, said, event = "failed", f"runtime unavailable: {exc}", _event(row.verb, "failed")
        except Exception as exc:  # a decision path must never guess; it records
            state, said, event = "failed", f"{type(exc).__name__}: {exc}"[:400], _event(row.verb, "failed")
        self._finish(row, state, said, event, identity.identity if identity else "")

    def _execute(self, row: ActionRow, identity: Identity | None) -> tuple[str, str, str]:
        handle, verb = row.handle, row.verb
        if verb in ("spawn", "wake"):
            if identity is None:
                return "refused", f"no identity/channel recorded for {handle}; cannot {verb}", "REFUSED"
            # The decision was made from facts read at `act()` time; this thread
            # runs later, so the world is re-read before the one call that makes a
            # pod (Bomu's D12-10e2: a direct `_execute` must not run a second one).
            existing = self.runtime.inspect_container(handle)
            if existing is not None and existing.alive:
                said = (f"{handle} is already alive; nothing to wake" if verb == "wake"
                        else f"{handle} is already alive; nothing to spawn (existing pod)")
                return "succeeded", said, "SPAWN"
            if self._spec_for is None:
                return ("failed",
                        f"{verb} cannot run: no pod renderer is configured for {handle} "
                        "(spec_for is not wired)", "SPAWN_FAILED")
            facts = self.runtime.run(self._spec_for(handle, identity))
            self.store.record_launch(handle, boot_id=self.boot_id, identity=identity.identity,
                                     state="alive")
            where = f" at {facts.ip}" if facts.ip else ""
            done = "spawned" if verb == "spawn" else "woke"
            return "succeeded", f"{done} {handle}{where}", "SPAWN"

        if verb == "freeze":
            self._agent_control.request_standdown(handle)
            waited = self._wait_absent(handle)
            if waited == "unreadable":
                # F-8: no verdict arrived; that is `timeout`, not `failed` (L-02).
                return ("timeout",
                        f"runtime could not be read while waiting for {handle} to stand down; "
                        "the outcome is unknown", "FREEZE")
            if waited != "absent":
                return ("timeout", f"stand-down did not complete for {handle}; the pod is still "
                                   "present and the outcome is unknown", "FREEZE")
            report = self.ledger.revoke_all(handle, "teardown")
            # F-1: the core released this pod; the agent did not report an ending.
            # `idle` is the session processor's word and must not be borrowed here.
            self.store.record_launch(handle, boot_id=self.boot_id,
                                     identity=identity.identity if identity else "",
                                     state="frozen", ending="standdown")
            said = f"froze {handle}: pod released, work preserved"
            if report.failed:
                said += f"; {report.failed} credential(s) could not be revoked"
            return "succeeded", said, "FREEZE"

        if verb == "rescue":
            self._agent_control.request_rescue(handle)
            waited = self._wait_absent(handle)
            if waited == "unreadable":
                # F-8: the same closed definition applies to rescue.
                return ("timeout",
                        f"runtime could not be read while waiting for {handle} to rescue; "
                        "the outcome is unknown", "RESCUE")
            if waited != "absent":
                return ("timeout", f"rescue did not complete for {handle}; the pod is still "
                                   "present and the outcome is unknown", "RESCUE")
            report = self.ledger.revoke_all(handle, "relaunch")
            self.store.record_launch(handle, boot_id=self.boot_id,
                                     identity=identity.identity if identity else "",
                                     state="rescued", ending="rescued")
            said = (f"rescued {handle}; workspace preserved, credentials revoked "
                    "(new credentials are minted at the next wake)")
            if report.failed:
                said += f"; {report.failed} credential(s) could not be revoked"
            return "succeeded", said, "RESCUE"

        if verb == "restart-ui":
            # L-10. Replace the pod: the old process dies with the sessions it
            # held in memory, and the new one starts with none. Nothing is revoked
            # in place and nothing is asked of the UI's store — a pod replacement
            # invalidates by construction, which is stronger than a hop into
            # another process's memory (D-12's trust domain). The action row is
            # the witness, not the response: the caller's own session dies here,
            # so it will never poll `succeeded`.
            if self._ui_spec is None:
                return ("failed",
                        f"restart-ui cannot run: no UI pod renderer is configured for {UI_POD} "
                        "(ui_spec is not wired); the old pod was not touched",
                        "UI_RESTART_FAILED")
            try:
                self.runtime.remove(UI_POD)
            except RuntimeUnavailableError as exc:
                return ("failed",
                        f"restart-ui did not replace {UI_POD}: the old pod could not be removed: {exc}",
                        "UI_RESTART_FAILED")
            try:
                facts = self.runtime.run(self._ui_spec())
            except Exception as exc:  # the old pod is already gone; say both facts
                return ("failed",
                        f"restart-ui removed the old {UI_POD} pod but the new one did not start "
                        f"({type(exc).__name__}: {exc}); the UI is down and its old sessions "
                        "are gone",
                        "UI_RESTART_FAILED")
            where = f" at {facts.ip}" if facts.ip else ""
            return ("succeeded",
                    f"replaced {UI_POD}{where}; sessions minted before it died with the old "
                    "process (UI-initiated, Manager-executed, no channel)",
                    "UI_RESTART")

        if verb == "destroy":
            report = self.ledger.revoke_all(handle, "teardown")
            partial = report.failed > 0
            mesh_note = ""
            if self._mesh_freeze is not None:
                try:
                    if not self._mesh_freeze(handle):
                        mesh_note = "; mesh peer freeze not confirmed"
                        partial = True
                except Exception as exc:
                    mesh_note = f"; mesh peer freeze failed ({type(exc).__name__})"
                    partial = True
            else:
                # Named gap, not a silent skip: network.py has not landed.
                mesh_note = "; mesh peer freeze not wired (network.py not landed)"
            try:
                self.runtime.remove(handle)
            except RuntimeUnavailableError as exc:
                return "failed", f"destroy of {handle} did not remove the pod: {exc}", "TEARDOWN_PARTIAL"
            self.store.record_launch(handle, boot_id=self.boot_id,
                                     identity=identity.identity if identity else "",
                                     state="inactive", ending="destroyed")
            said = f"destroyed {handle}; credentials revoked"
            if report.failed:
                said += f" except {report.failed} of {report.total}{mesh_note}"
            else:
                said += mesh_note
            return ("failed" if partial else "succeeded"), said, (
                "TEARDOWN_PARTIAL" if partial else "TEARDOWN")

        return "refused", f"unknown action {verb!r}", "REFUSED"

    # -- small helpers --------------------------------------------------
    def _finish(self, row: ActionRow, state: str, said: str, event: str, identity: str = "") -> None:
        if state not in TERMINAL:
            # F-6: an unknown verdict word is a defect, not something to coerce.
            raise ValueError(f"the core produced a non-terminal verdict {state!r}; refusing to write it")
        self.store.finish_action(row.id, state=state, said=said, at=self.clock(),
                                 event=event, identity=identity)

    def _refuse(self, handle: str, action: str, *, actor: str, actor_source: str, reason: str,
                detail: str, identity: str = "") -> str:
        created = self.clock()
        row = self.store.create_action(
            action_id=secrets.token_hex(12), handle=handle, verb=action, actor=actor,
            actor_source=actor_source, reason=reason, boot_id=self.boot_id, at=created,
        )
        self.store.finish_action(row.id, state="refused", said=detail, at=created,
                                 event="REFUSED", identity=identity)
        return row.id

    def _settle(self, handle: str, action: str, actor: str, actor_source: str, reason: str,
                state: str, said: str, identity: str = "") -> str:
        created = self.clock()
        row = self.store.create_action(
            action_id=secrets.token_hex(12), handle=handle, verb=action, actor=actor,
            actor_source=actor_source, reason=reason, boot_id=self.boot_id, at=created,
        )
        event = "SPAWN" if action in ("spawn", "wake") else _event(action, state)
        self.store.finish_action(row.id, state=state, said=said, at=created,
                                 event=event, identity=identity)
        return row.id

    def _wait_absent(self, handle: str) -> str:
        """'absent' | 'present' | 'unreadable' — polled to the deadline.

        An unread runtime is not a verdict: the wait keeps trying until the
        deadline, so `timeout` means the deadline passed with no answer, not
        that the first read failed (F-8's clock note).
        """
        deadline = _parse(self.clock()) + timedelta(seconds=self.deadline)
        unreadable = ""
        while True:
            try:
                facts = self.runtime.inspect_container(handle)
            except RuntimeUnavailableError as exc:
                unreadable = str(exc)
            else:
                unreadable = ""
                if facts is None:
                    return "absent"
            if _parse(self.clock()) >= deadline:
                return "unreadable" if unreadable else "present"
            self._sleep(POLL_S)

    def _expires_at(self, created: str) -> str:
        return (_parse(created) + timedelta(seconds=self.deadline)).isoformat()

    def _checkouts(self, handle: str) -> tuple[model.Checkout, ...]:
        rows = self.store.credentials(instance=handle, state="open")
        return tuple(
            model.Checkout(id=row.id, scope=row.scope, issued_at=row.issued_at,
                           expires_at=row.expires_at or "", unrevocable=row.unrevocable)
            for row in rows
        )

    # -- boot (C-06 / R-04) ---------------------------------------------
    def boot_reconcile(self) -> dict[str, object]:
        """Conservative in both directions. Unreadable runtime = nothing revoked."""
        try:
            live: set[str] = set()
            for facts in self.runtime.list_agents():
                live.add(facts.labels.get(HANDLE_LABEL) or facts.name)
            readable = True
        except RuntimeUnavailableError:
            live = set()
            readable = False
        report = self.ledger.reconcile_boot(boot_id=self.boot_id, live_instances=live,
                                            runtime_readable=readable)
        return {
            "boot_id": self.boot_id, "runtime_readable": readable,
            "revoked": report.revoked, "failed": report.failed,
            "unrevocable": report.unrevocable, "skipped": report.skipped,
            "total": report.total,
        }


def _observed_state(facts: ContainerFacts) -> str:
    if facts.state == "running":
        return "alive"
    if facts.state == "paused":
        return "frozen"  # M4: a paused process is a different freeze; provenance says so
    if facts.state in ("created", "starting"):
        return "starting"
    if facts.state in ("exited", "dead", "stopped"):
        return "dead"
    return "unknown"


def _provenance(facts: ContainerFacts) -> dict[str, str]:
    if facts.state == "paused":
        return {"podman": "paused",
                "note": "paused process; this is not the product's freeze (work is in memory)"}
    return {"podman": facts.state}


def _asked(actor: str, source: str) -> str:
    """The actor with its provenance (F-7); a name alone is a string."""
    return f"asked by {actor} via {source}" if source else f"asked by {actor}"


def _event(verb: str, state: str) -> str:
    if state == "refused":
        return "REFUSED"
    if verb in ("spawn", "wake"):
        return "SPAWN" if state == "succeeded" else "SPAWN_FAILED"
    if verb == "freeze":
        return "FREEZE"
    if verb == "rescue":
        return "RESCUE"
    if verb == "destroy":
        return "TEARDOWN" if state == "succeeded" else "TEARDOWN_PARTIAL"
    if verb == "restart-ui":
        return "UI_RESTART" if state == "succeeded" else "UI_RESTART_FAILED"
    return "REFUSED"


def manager_for(cfg: object = None) -> NativeManager:
    """The Office interface's entry point: one function, one product.

    The runtime socket location is this adapter's configuration, never an
    argument the Office surface can set. Without a pod renderer or an agent
    stand-down port the core still runs, and the verbs that need them fail with
    a named reason instead of pretending.
    """
    from babo.config.loader import load_config

    config = cfg if cfg is not None else load_config()
    town = Path(config.baton.town_path)  # type: ignore[attr-defined]
    store = Store(town / "state" / "office-manager.sqlite3")
    ledger = CredentialLedger(store, clock=now_iso)
    return NativeManager(store=store, runtime=PodmanRuntime(), ledger=ledger)
