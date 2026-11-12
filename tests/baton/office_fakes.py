"""A deterministic double for the Office interface — a test fixture, not a core.

It exists so the product's own surfaces (join, control, routes, CLI) can be
tested before the manager slice lands. It implements `babo.office.api.
OfficeManager` and nothing else: no lifecycle policy, no persistence, no
credentials. When the core lands, these tests keep working with `FakeCore` and
the core gets its own suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from babo.office.api import ActionRecord
from babo.office.model import AgentObserved


class CoreRefusalError(Exception):
    """Named to match the core's own refusal type, so the hop maps it to 409."""


class UnknownActionError(Exception):
    """Named to match the core's unknown-id type, so the hop maps it to 404."""


@dataclass
class FakeCore:
    name: str = "fake"
    agents_by_handle: dict[str, AgentObserved] = field(default_factory=dict)
    fail: set[str] = field(default_factory=set)  # action names that come back `failed`
    unavailable: str = ""
    actions: dict[str, ActionRecord] = field(default_factory=dict)
    audit: list[tuple[str, str, str, str]] = field(default_factory=list)
    _next: int = 0

    def health(self) -> tuple[bool, str]:
        return (False, self.unavailable) if self.unavailable else (True, "fake core")

    def agents(self) -> list[AgentObserved]:
        if self.unavailable:
            from babo.office.api import CoreNotBuiltError

            raise CoreNotBuiltError(self.unavailable)
        return list(self.agents_by_handle.values())

    def act(self, handle: str, action: str, reason: str, *,
            actor: str = "", actor_source: str = "", action_id: str | None = None) -> str:
        """Record the row the way the real core does, including who asked and why.

        F-7 (soro): a name without provenance is a string that satisfied a check,
        not an identity. The fixture carries both onto the row so the hop's read
        path is tested against a shape the product can actually produce.
        """
        who = {"actor": actor, "actor_source": actor_source, "reason": reason}
        if not actor or not actor_source:
            # R5: no actor, no effect — refused by name, not "failed".
            self._next += 1
            refused_id = f"act-{self._next}"
            self.actions[refused_id] = ActionRecord(
                id=refused_id, handle=handle, action=action, state="refused",
                said=f"refused: {action} on {handle} names no actor", **who)
            return refused_id
        self.audit.append((handle, action, actor, actor_source))
        if action_id and action_id in self.actions:
            existing = self.actions[action_id]
            if existing.handle != handle or existing.action != action:
                # The core does not raise here: it records a refused row and hands
                # back its id, so the UI reads the refusal like every other one.
                self.actions[action_id] = ActionRecord(
                    id=action_id, handle=existing.handle, action=existing.action, state="refused",
                    said=f"action {action_id} is already pending for {existing.handle} ({existing.action}); not accepted",
                    actor=existing.actor, actor_source=existing.actor_source, reason=existing.reason)
                return action_id
            return action_id
        if self.unavailable:
            from babo.office.api import CoreNotBuiltError

            raise CoreNotBuiltError(self.unavailable)
        if handle not in self.agents_by_handle:
            # Refusal is a state the core reports, not an exception: nothing ran.
            self._next += 1
            refused_id = f"act-{self._next}"
            self.actions[refused_id] = ActionRecord(
                id=refused_id, handle=handle, action=action, state="refused",
                said=f"no agent called {handle} is recorded here; record it first", **who)
            return refused_id
        if action_id:
            self.actions[action_id] = ActionRecord(id=action_id, handle=handle, action=action, **who)
            return action_id
        self._next += 1
        new_id = f"act-{self._next}"
        self.actions[new_id] = ActionRecord(id=new_id, handle=handle, action=action, **who)
        return new_id

    def action(self, action_id: str) -> ActionRecord:
        record = self.actions.get(action_id)
        if record is None:
            raise UnknownActionError(f"no such action: {action_id}")
        if record.terminal:
            return record
        handle, action = record.handle, record.action
        if action in self.fail:
            settled = replace(record, state="failed", said=f"fake: {action} {handle} failed at the runtime")
        elif action == "wake":
            settled = replace(record, state="succeeded", said=f"woke {handle}")
            self._set_state(handle, "alive")
        elif action == "freeze":
            settled = replace(record, state="succeeded",
                              said=f"froze {handle}: stood down, work pushed, pod released")
            self._set_state(handle, "frozen")
        elif action == "restart-ui":
            settled = replace(record, state="succeeded", said="replaced the UI pod; sessions invalidated")
        else:
            settled = replace(record, state="refused", said=f"fake doesn't run '{action}'")
        self.actions[action_id] = settled
        return settled

    def _set_state(self, handle: str, state: str) -> None:
        current = self.agents_by_handle[handle]
        self.agents_by_handle[handle] = AgentObserved(
            handle=handle, state=state, wg_address=current.wg_address,
            last_activity_at=current.last_activity_at, checkouts=current.checkouts,
            provenance=current.provenance)
