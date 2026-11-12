"""The office's control plane in one place: read the live view, change what's
configured, and run one lifecycle action without lying about it.

Reads never cache: every ``office_view`` asks the Manager again and joins the
answer with the desired records. Actions follow the same rule — the Manager's
own words are the result (``succeeded``/``failed``/``refused``/``timeout``,
never a client-side guess), and nothing about the configuration changes unless
the Manager said it happened.

The two verbs here are the Manager's, verbatim: ``wake`` and ``freeze``. A
freeze asks the agent to stand down and save its work, then releases the pod
and revokes its credentials; a wake brings a fresh pod up on the rescued
workspace with new keys. The work survives; the process does not — the page
must say exactly that.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any, cast

from babo.baton import projects as project_board
from babo.baton.town import BatonError, Town
from babo.office import model
from babo.office.actors import Actor
from babo.office.api import (
    AGENT_ACTIONS,
    OPERATOR_ACTIONS,
    TERMINAL,
    ActionRecord,
    CoreNotBuiltError,
    OfficeManager,
)
from babo.office.store import AgentStore, CrewStore

# AGENT_ACTIONS and TERMINAL are the interface's vocabulary, imported — not restated.
# A second literal tuple here would drift the moment the interface changed, and
# the page would keep accepting a verb the core no longer had. The terminal path
# carries agent verbs only: the pod verb (`restart-ui`) has no terminal door, and
# the interface declares that narrowing itself, so nobody here has to know it.


@dataclass
class Op:
    """One lifecycle action, from the moment it's asked for to the Manager's verdict."""

    id: str
    handle: str
    action: str
    state: str = "pending"  # pending | running | succeeded | failed | refused | timeout
    said: str = ""
    observed: dict[str, object] | None = None

    def overview(self) -> dict[str, object]:
        return {"id": self.id, "handle": self.handle, "action": self.action, "state": self.state,
                "said": self.said, "observed": self.observed}


@dataclass
class Ops:
    """The gateway's short memory of actions in flight (never persisted: the Manager's record is the record)."""

    by_id: dict[str, Op] = field(default_factory=lambda: cast(dict[str, Op], {}))
    limit: int = 100

    def add(self, action_id: str, handle: str, action: str) -> Op:
        op = Op(id=action_id, handle=handle, action=action)
        self.by_id[action_id] = op
        while len(self.by_id) > self.limit:
            self.by_id.pop(next(iter(self.by_id)))
        return op


def office_view(town: Town, core: OfficeManager, listener: str = "") -> dict[str, object]:
    """Projects with their crews, plus every managed agent joined to the Manager's answer.

    If the Manager can't be read, the page is told exactly that — the desired
    records are still listed, each marked unknown, never shown as if running.
    """
    crews = CrewStore(town)
    store = AgentStore(town)
    desired = store.all()

    try:
        observed_list = core.agents()
        reachable, detail = core.health()
        unavailable = "" if reachable else detail
    except CoreNotBuiltError as exc:
        observed_list = []
        unavailable = str(exc)
    observed = {seen.handle: seen for seen in observed_list}

    live: list[model.AgentLive] = []
    for handle, entry in sorted(desired.items()):
        record = model.AgentDesired(
            handle=handle,
            resident=str(entry.get("resident") or ""),
            project=str(entry.get("project") or ""),
            crew=str(entry.get("crew") or ""),
            desired=str(entry.get("desired") or "running"),
            labels=dict(cast(dict[str, str], entry.get("labels") or {})),
            created=str(entry.get("created") or ""),
            updated=str(entry.get("updated") or ""),
        )
        if unavailable:
            live.append(model.AgentLive(handle=handle, resident=record.resident, project=record.project,
                                        crew=record.crew, status=model.UNKNOWN, desired=record.desired,
                                        said=unavailable))
        else:
            live.append(model.join(record, observed.get(handle)))
    for handle, seen in sorted(observed.items()):
        if handle not in desired:
            live.append(model.join(None, seen))

    roles = {slug: model.Crew(project=slug, lead=str(crew.get("lead") or ""),
                               crew=tuple(cast(list[str], crew.get("crew") or [])))
             for slug, crew in crews.all().items()}

    projects: list[dict[str, object]] = []
    for project in project_board.all_projects(town):
        crew = crews.read(project.slug)
        projects.append({
            "slug": project.slug,
            "title": project.title,
            "project_lead": project.lead,
            "status": project.status,
            "help_wanted": project.help_wanted,
            "open_tickets": len([t for t in project.tickets if t.status in project_board.OPEN]),
            "lead": crew.get("lead", ""),
            "crew": crew.get("crew", []),
            "crew_recorded": crew.get("exists", False),
            "agents": [a.handle for a in live if a.project == project.slug],
        })

    live = [replace(a, role=roles[a.project].role_of(a.handle) if a.project in roles else "") for a in live]

    return {
        "projects": projects,
        "agents": [a.overview() for a in live],
        "missing": [a.handle for a in live if a.status == model.MISSING],
        "drifted": [a.handle for a in live if a.status == model.DRIFTED],
        "unmanaged": [a.handle for a in live if a.status == model.UNMANAGED],
        "manager": {"name": getattr(core, "name", "unknown"), "available": not unavailable, "said": unavailable},
        # Corroboration only: the Router's source rule is the wall; a bind address is a fact,
        # not an authentication check.
        "listener": listener,
        "desired": {handle: dict(entry) for handle, entry in desired.items()},
    }


def change_crew(town: Town, change: dict[str, Any]) -> dict[str, object]:
    said = CrewStore(town).write(change)
    project = str(change.get("project") or "")
    return {"message": said, "crew": CrewStore(town).read(project)}


def record_agent(town: Town, change: dict[str, Any]) -> dict[str, object]:
    said = AgentStore(town).record(town, change)
    handle = str(change.get("handle") or change.get("agent") or "").strip().lower()
    return {"message": said, "agent": AgentStore(town).get(handle)}


def _settle(core: OfficeManager, op: Op, store: AgentStore, record: ActionRecord) -> None:
    """The one place an outcome becomes product state, for both doors.

    The page's follow-up and the CLI's wait come through here, so a success
    moves the desired record the same way whichever asked for it — otherwise a
    CLI wake leaves the pair rendering as `drifted`, a product bug wearing an
    operator's hat.
    """
    op.state, op.said = record.state, record.said
    if record.state != "succeeded":
        return
    try:
        observed = core.agents()
    except CoreNotBuiltError:
        observed = []
    seen = next((a for a in observed if a.handle == op.handle), None)
    op.observed = seen.overview() if seen else None
    # Only after the core says it happened does the configuration follow.
    try:
        store.set_desired(op.handle, "running" if op.action == "wake" else "frozen")
    except BatonError:
        pass


async def _poll(core: OfficeManager, op: Op, timeout_s: float, store: AgentStore) -> None:
    """Follow the core's action record until it settles, in its own words."""
    waited = 0.0
    while True:
        try:
            row = await asyncio.to_thread(core.action, op.id)
        except CoreNotBuiltError as exc:
            # Gate off is a refusal, not a failure: nothing ran, and the core says why.
            op.state, op.said = "refused", str(exc)
            return
        if row.state in TERMINAL:
            _settle(core, op, store, row)
            return
        op.state = "running" if row.state == "running" else "pending"
        if waited >= timeout_s:
            op.state, op.said = "timeout", f"no verdict from the manager core after {timeout_s:g}s"
            return
        await asyncio.sleep(POLL_S)
        waited += POLL_S


POLL_S = 0.5


def perform_sync(core: OfficeManager, handle: str, verb: str, reason: str, *, store: AgentStore,
                 actor: Actor, timeout_s: float = 120.0) -> Op:
    """The terminal's path: ask, then wait for the core's verdict (no event loop).

    The same words the page shows — pending never becomes success by itself —
    and the same settle helper, so a CLI success moves the desired record too.
    """
    import time

    if verb in OPERATOR_ACTIONS:
        # A local terminal process is not the host-WG door. The verb exists; the
        # door does not (ruled 2026-11-11).
        raise BatonError(f"'{verb}' is the host operator's verb, admitted at the host-WG door; "
                         "this terminal process is not that door")
    if verb not in AGENT_ACTIONS:
        raise BatonError(f"there's no '{verb}' action; it is one of: {', '.join(AGENT_ACTIONS)}")
    try:
        action_id = core.act(handle, verb, reason, actor=actor.name, actor_source=actor.source)
    except CoreNotBuiltError as exc:
        raise BatonError(str(exc)) from exc
    op = Op(id=action_id, handle=handle, action=verb)
    waited = 0.0
    while True:
        try:
            row: ActionRecord = core.action(action_id)
        except CoreNotBuiltError as exc:
            op.state, op.said = "refused", str(exc)
            return op
        if row.state in TERMINAL:
            _settle(core, op, store, row)
            return op
        if waited >= timeout_s:
            op.state, op.said = "timeout", f"no verdict from the manager core after {timeout_s:g}s"
            return op
        time.sleep(POLL_S)
        waited += POLL_S


def begin(town: Town, core: OfficeManager, ops: Ops, handle: str, verb: str, reason: str,
          *, actor: Actor, action_id: str | None = None) -> Op:
    """Ask the core for one action and start following it. Pending until it answers."""
    if verb in OPERATOR_ACTIONS:
        raise BatonError(f"'{verb}' is the host operator's verb, admitted at the host-WG door; "
                         "this terminal process is not that door")
    if verb not in AGENT_ACTIONS:
        raise BatonError(f"there's no '{verb}' action; it is one of: {', '.join(AGENT_ACTIONS)}")
    store = AgentStore(town)
    # No local "is it recorded?" gate: that precondition is the core's (L-03) and
    # it refuses by name, so both doors get one refusal instead of two behaviours.
    try:
        core_id = core.act(handle, verb, reason, actor=actor.name, actor_source=actor.source,
                           action_id=action_id)
    except CoreNotBuiltError as exc:
        raise BatonError(str(exc)) from exc
    op = ops.add(core_id, handle, verb)
    asyncio.create_task(_poll(core, op, timeout_s=120.0, store=store))
    return op


def op_view(ops: Ops, op_id: str) -> dict[str, object]:
    op = ops.by_id.get(op_id)
    if op is None:
        raise BatonError("that action isn't in this gateway's memory (it may have restarted); "
                         "read the office again for where things stand")
    return op.overview()
