"""The native core's lifecycle rules, proven without a pod.

Each case names the frozen entry it holds the code to. Nothing here claims a
served proof; the runtime is a recording double.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from babo.office.api import ActionRecord
from babo.office.audit import Store
from babo.office.core import (
    HANDLE_LABEL,
    MANAGED_LABEL,
    CoreRefusalError,
    Identity,
    NativeManager,
    UnknownActionError,
    decide,
)
from babo.office.credentials import CredentialLedger, FileAdapter, Minted, TokenAdapter
from babo.office.runtime import ContainerFacts, LaunchSpec
from baton.office_core_fakes import RecordingControl, RecordingRuntime, StubbornControl

NOW = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731 - a clock, not a constant


def _ledger(tmp_path: Path) -> CredentialLedger:
    ledger = CredentialLedger(Store(tmp_path / "m.db"), clock=NOW)
    ledger.registry.register(FileAdapter(tmp_path / "creds"))
    return ledger


def _manager(tmp_path: Path, runtime: RecordingRuntime, **kw) -> NativeManager:
    kw.setdefault("identity_of", lambda handle: Identity("kimi", "project-x"))
    kw.setdefault(
        "spec_for",
        lambda handle, identity: LaunchSpec(
            name=handle, image="babo-agent:test",
            labels={MANAGED_LABEL: "true", HANDLE_LABEL: handle},
        ),
    )
    kw.setdefault("executor", lambda fn: fn())
    deadline = kw.pop("deadline", 5.0)
    return NativeManager(store=Store(tmp_path / "m.db"), runtime=runtime,
                         ledger=_ledger(tmp_path), clock=NOW, deadline=deadline, **kw)


def _alive(handle: str = "agent-a") -> ContainerFacts:
    return ContainerFacts(name=handle, state="running", ip="10.89.0.42",
                          labels={MANAGED_LABEL: "true", HANDLE_LABEL: handle})


def test_unknown_verb_is_refused_before_a_row_or_a_runtime_call(tmp_path: Path) -> None:
    # L-01: validation happens before a row exists or a thread starts.
    runtime = RecordingRuntime()
    manager = _manager(tmp_path, runtime)
    with pytest.raises(CoreRefusalError) as caught:
        manager.act("agent-a", "explode", "because", actor="evans", actor_source="terminal")
    assert "it is one of spawn, wake, freeze, rescue, destroy, restart-ui" in str(caught.value)
    assert manager.store.actions() == []
    assert runtime.calls == []


def test_no_actor_is_refused_and_the_refusal_is_durable(tmp_path: Path) -> None:
    # A-03 / R5 / U-03: refused by name, visible at the surface, no effect.
    runtime = RecordingRuntime()
    manager = _manager(tmp_path, runtime)
    action_id = manager.act("agent-a", "freeze", "because", actor="")
    record = manager.action(action_id)
    assert record.state == "refused"
    assert "no approval recorded for agent-a (asked by <none>)" in record.said
    assert runtime.calls == []
    assert manager.store.audit(event="REFUSED")[0].actor == "<none>"


def test_an_actor_without_provenance_is_refused_with_its_own_detail(tmp_path: Path) -> None:
    # F-7 as a gate: the name alone is not an identity.
    manager = _manager(tmp_path, RecordingRuntime())
    record = manager.action(manager.act("agent-a", "freeze", "because", actor="owner"))
    assert record.state == "refused"
    assert "no actor source recorded for agent-a (asked by owner)" in record.said
    row = manager.store.get_action(record.id)
    assert row is not None and row.actor == "owner" and row.actor_source == ""


def test_no_reason_is_refused_and_names_the_actor(tmp_path: Path) -> None:
    manager = _manager(tmp_path, RecordingRuntime())
    record = manager.action(manager.act("agent-a", "freeze", "  ", actor="evans", actor_source="terminal"))
    assert record.state == "refused"
    assert "no reason recorded for agent-a (asked by evans via terminal)" in record.said


def test_refused_actions_leave_no_podman_effect(tmp_path: Path) -> None:
    # L-03 acceptance: the runtime double records calls, and only the inspect happened.
    runtime = RecordingRuntime()
    manager = _manager(tmp_path, runtime)
    record = manager.action(manager.act("agent-a", "freeze", "because", actor="evans", actor_source="terminal"))
    assert record.state == "refused"
    assert record.said == "nothing to freeze: no pod for agent-a"
    assert runtime.calls == [("inspect", "agent-a")]


def test_freeze_on_a_pod_that_is_not_alive_names_the_state(tmp_path: Path) -> None:
    runtime = RecordingRuntime(facts={"agent-a": ContainerFacts(name="agent-a", state="paused")})
    manager = _manager(tmp_path, runtime)
    record = manager.action(manager.act("agent-a", "freeze", "because", actor="evans", actor_source="terminal"))
    assert record.state == "refused"
    assert record.said == "agent-a is paused, not alive; nothing to freeze"


def test_wake_on_an_awake_agent_succeeds_with_its_own_detail(tmp_path: Path) -> None:
    # L-07(b): a success that says so, not an error and not a fake restart.
    runtime = RecordingRuntime(facts={"agent-a": _alive()})
    manager = _manager(tmp_path, runtime, identity_of=None)
    record = manager.action(manager.act("agent-a", "wake", "check", actor="evans", actor_source="terminal"))
    assert record.state == "succeeded"
    assert record.said == "agent-a is already alive; nothing to wake"
    assert ("run", "agent-a") not in runtime.calls


def test_spawn_on_an_awake_agent_resolves_to_the_existing_pod(tmp_path: Path) -> None:
    # L-07: spawn short-circuits on an alive pod instead of running a second
    # `podman run`; the detail says which happened.
    runtime = RecordingRuntime(facts={"agent-a": _alive()})
    manager = _manager(tmp_path, runtime, identity_of=None)
    record = manager.action(manager.act("agent-a", "spawn", "check", actor="evans",
                                        actor_source="terminal"))
    assert record.state == "succeeded"
    assert "already alive; nothing to spawn (existing pod)" in record.said
    assert ("run", "agent-a") not in runtime.calls


def test_spawn_on_a_starting_record_does_not_create_a_second_pod(tmp_path: Path) -> None:
    # L-07 / spawner.rb:18-25: a starting record resolves to `starting: true`.
    runtime = RecordingRuntime()
    manager = _manager(tmp_path, runtime)
    manager.store.record_launch("agent-a", boot_id="b1", identity="kimi", state="starting")
    record = manager.action(manager.act("agent-a", "spawn", "check", actor="evans",
                                        actor_source="terminal"))
    assert record.state == "succeeded"
    assert "starting: true" in record.said
    assert runtime.calls == [("inspect", "agent-a")]


def test_spawn_on_an_alive_record_resolves_before_the_identity_gate(tmp_path: Path) -> None:
    # L-07: the record is the core's own knowledge that a pod was already made,
    # so it resolves the verb with no `run` and no identity re-ask.
    runtime = RecordingRuntime()
    manager = _manager(tmp_path, runtime, identity_of=None)
    manager.store.record_launch("agent-a", boot_id="b1", identity="kimi", state="alive")
    record = manager.action(manager.act("agent-a", "spawn", "check", actor="evans",
                                        actor_source="terminal"))
    assert record.state == "succeeded"
    assert "existing pod" in record.said
    assert runtime.calls == [("inspect", "agent-a")]


def test_spawn_without_an_identity_or_channel_is_refused(tmp_path: Path) -> None:
    manager = _manager(tmp_path, RecordingRuntime(), identity_of=None)
    record = manager.action(manager.act("agent-a", "spawn", "because", actor="evans", actor_source="terminal"))
    assert record.state == "refused"
    assert record.said == "no identity/channel recorded for agent-a; cannot spawn"


def test_spawn_records_a_launch_and_an_audit_row(tmp_path: Path) -> None:
    runtime = RecordingRuntime()
    manager = _manager(tmp_path, runtime)
    record = manager.action(manager.act("agent-a", "spawn", "first", actor="evans", actor_source="terminal"))
    assert record.state == "succeeded"
    assert record.said == "spawned agent-a at 10.89.0.42"
    launch = manager.store.launch("agent-a")
    assert launch is not None and launch["state"] == "alive"
    audit = manager.store.audit(event="SPAWN")
    assert len(audit) == 1 and audit[0].actor == "evans" and audit[0].reason == "first"


def test_a_replayed_final_action_is_a_no_op_with_the_original_detail(tmp_path: Path) -> None:
    manager = _manager(tmp_path, RecordingRuntime())
    first = manager.act("agent-a", "spawn", "first", actor="evans", actor_source="terminal")
    again = manager.act("agent-a", "spawn", "first", actor="evans", actor_source="terminal", action_id=first)
    assert again == first
    assert len(manager.store.actions()) == 1
    assert manager.action(again).said == "spawned agent-a at 10.89.0.42"


def test_a_replay_key_cannot_be_reused_for_another_handle_or_verb(tmp_path: Path) -> None:
    manager = _manager(tmp_path, RecordingRuntime())
    first = manager.act("agent-a", "spawn", "first", actor="evans", actor_source="terminal")
    with pytest.raises(CoreRefusalError) as caught:
        manager.act("agent-b", "spawn", "first", actor="evans", actor_source="terminal", action_id=first)
    assert "belongs to (agent-a, spawn)" in str(caught.value)


def test_two_concurrent_spawns_make_one_pod_and_one_refusal(tmp_path: Path) -> None:
    # L-07(a) / D-6: the per-handle single-flight is what makes "one effect" true.
    queued: list[object] = []
    runtime = RecordingRuntime()
    manager = _manager(tmp_path, runtime, executor=queued.append)
    first = manager.act("agent-a", "spawn", "first", actor="evans", actor_source="terminal")
    second = manager.act("agent-a", "spawn", "again", actor="evans", actor_source="terminal")
    assert manager.action(first).state == "pending"
    duplicate = manager.action(second)
    assert duplicate.state == "refused"
    assert f"action {first} is already pending for agent-a (spawn); not accepted" in duplicate.said
    queued[0]()  # let the one accepted action run
    assert [call for call in runtime.calls if call[0] == "run"] == [("run", "agent-a")]
    assert manager.action(first).state == "succeeded"


def test_freeze_uses_the_agent_handshake_never_a_signal(tmp_path: Path) -> None:
    # L-06 / soro's note: the stand-down is an agent request; the runtime only
    # reports absence afterwards. No 'signal' exists on the adapter at all.
    runtime = RecordingRuntime(facts={"agent-a": _alive()})
    control = RecordingControl(runtime)
    manager = _manager(tmp_path, runtime, agent_control=control)
    record = manager.action(manager.act("agent-a", "freeze", "stand down", actor="evans", actor_source="terminal"))
    assert record.state == "succeeded"
    assert control.calls == ["standdown:agent-a"]
    assert manager.store.launch("agent-a")["ending"] == "standdown"
    assert manager.store.audit(event="FREEZE")[0].state == "succeeded"


def test_a_stand_down_that_never_completes_is_timeout_not_failed(tmp_path: Path) -> None:
    # L-05: ran past its deadline, outcome unknown — never collapsed into failure.
    runtime = RecordingRuntime(facts={"agent-a": _alive()})
    manager = _manager(tmp_path, runtime, agent_control=StubbornControl(), deadline=0.05,
                       sleep=lambda _s: None)
    record = manager.action(manager.act("agent-a", "freeze", "stand down", actor="evans", actor_source="terminal"))
    assert record.state == "timeout"
    assert "outcome is unknown" in record.said
    # the pod is still there and no credential was revoked on a guess
    assert runtime.facts["agent-a"].alive


def test_destroy_revokes_before_it_removes_and_names_a_partial_failure(tmp_path: Path) -> None:
    runtime = RecordingRuntime(facts={"agent-a": _alive()})
    manager = _manager(tmp_path, runtime)
    adapter = TokenAdapter(
        "generic:", lambda instance, scope: f"generic:https://{instance}.test/{scope}",
        lambda instance, scope: Minted(secret="s3cr3t"),
        lambda handle: (_ for _ in ()).throw(RuntimeError("endpoint down")),
    )
    manager.ledger.registry.register(adapter)
    row, _minted = manager.ledger.issue(adapter, "agent-a", "generic-token", boot_id="b1",
                                        launch_ref="b1:agent-a")
    # make removal fail: if revocation had not already run, the row would stay open
    from babo.office.runtime import RuntimeUnavailableError

    def _gone(name: str) -> None:
        raise RuntimeUnavailableError("runtime gone")

    runtime.remove = _gone  # type: ignore[method-assign]
    record = manager.action(manager.act("agent-a", "destroy", "end it", actor="evans", actor_source="terminal"))
    assert record.state == "failed"
    assert "did not remove the pod" in record.said
    assert manager.store.get_credential(row.id).state == "open"  # revoke tried, failed honestly
    assert manager.store.audit(event="CREDENTIAL_REVOKE_PARTIAL")


def test_destroy_success_revokes_credentials_and_writes_one_teardown_row(tmp_path: Path) -> None:
    runtime = RecordingRuntime(facts={"agent-a": _alive()})
    manager = _manager(tmp_path, runtime)
    file_adapter = FileAdapter(tmp_path / "creds")
    manager.ledger.registry.register(file_adapter)
    row, minted = manager.ledger.issue(file_adapter, "agent-a", "ssh-key", boot_id="b1",
                                       launch_ref="b1:agent-a")
    assert Path(row.locator[len("file:"):]).read_text().strip() == minted.secret
    record = manager.action(manager.act("agent-a", "destroy", "end it", actor="evans", actor_source="terminal"))
    assert record.state == "succeeded"
    assert manager.store.get_credential(row.id).state == "revoked"
    assert not Path(row.locator[len("file:"):]).exists()
    teardown = manager.store.audit(event="TEARDOWN")
    assert len(teardown) == 1 and teardown[0].actor == "evans"


def test_agents_reads_the_runtime_and_keeps_unknown_provenance(tmp_path: Path) -> None:
    runtime = RecordingRuntime(unavailable="socket refused")
    manager = _manager(tmp_path, runtime)
    manager.store.record_launch("agent-a", boot_id="b1", identity="kimi", state="alive")
    (agent,) = manager.agents()
    assert agent.state == "unknown"
    assert agent.provenance["podman"] == "error"
    assert "socket refused" in agent.provenance["note"]


def test_a_destroyed_agent_is_omitted_so_the_join_can_render_missing(tmp_path: Path) -> None:
    # M7 / D-2: absence after a deliberate destroy must not read `dead`.
    runtime = RecordingRuntime()
    manager = _manager(tmp_path, runtime)
    manager.store.record_launch("agent-a", boot_id="b1", identity="kimi", state="inactive",
                                ending="destroyed")
    assert manager.agents() == []


@pytest.mark.parametrize(
    "state,ending,expected",
    [
        ("frozen", "standdown", "frozen"),  # core-initiated stand-down
        ("frozen", "", "frozen"),           # no ending recorded, state decides
        ("alive", "", "dead"),              # absence with no graceful end is still death
        ("x", "idle", "frozen"),            # a pre-fix row keeps its old meaning
    ],
)
def test_the_frozen_dead_discriminator_has_four_cases(
    tmp_path: Path, state: str, ending: str, expected: str
) -> None:
    manager = _manager(tmp_path, RecordingRuntime())
    manager.store.record_launch("agent-a", boot_id="b1", identity="kimi", state=state,
                                ending=ending)
    (agent,) = manager.agents()
    assert agent.state == expected


def test_an_exited_pod_without_a_graceful_end_reads_dead(tmp_path: Path) -> None:
    runtime = RecordingRuntime(facts={"agent-a": ContainerFacts(name="agent-a", state="exited")})
    manager = _manager(tmp_path, runtime)
    (agent,) = manager.agents()
    assert agent.state == "dead"


def test_actor_source_is_stored_with_the_name_and_shown_in_refusals(tmp_path: Path) -> None:
    # F-7: a name without provenance is a string that satisfied a check.
    manager = _manager(tmp_path, RecordingRuntime())
    record = manager.action(manager.act("agent-a", "freeze", "", actor="owner",
                                        actor_source="webui-session"))
    assert record.state == "refused"
    assert "(asked by owner via webui-session)" in record.said
    row = manager.store.get_action(record.id)
    assert row is not None and row.actor == "owner" and row.actor_source == "webui-session"
    audit = manager.store.audit(event="REFUSED")[0]
    assert (audit.actor, audit.actor_source) == ("owner", "webui-session")


def test_a_stand_down_the_core_released_does_not_borrow_the_agents_ending(tmp_path: Path) -> None:
    # F-1: `idle` is the session processor's word; the core says `standdown`.
    runtime = RecordingRuntime(facts={"agent-a": _alive()})
    manager = _manager(tmp_path, runtime, agent_control=RecordingControl(runtime),
                       executor=lambda fn: fn())
    manager.action(manager.act("agent-a", "freeze", "stand down", actor="evans", actor_source="terminal"))
    (agent,) = manager.agents()
    assert agent.state == "frozen"
    assert agent.provenance["ended_reason"] == "standdown"
    assert "core released it" in agent.provenance["note"]


def test_an_unreadable_runtime_during_the_wait_is_not_reported_as_still_present(
    tmp_path: Path,
) -> None:
    # F-2: unreadable and present are different answers.
    runtime = RecordingRuntime(facts={"agent-a": _alive()})

    class GoesDark:
        def request_standdown(self, handle: str) -> None:
            runtime.unavailable = "socket gone"

        def request_rescue(self, handle: str) -> None:
            runtime.unavailable = "socket gone"

    manager = _manager(tmp_path, runtime, agent_control=GoesDark(), deadline=0.05,
                       sleep=lambda _s: None)
    record = manager.action(manager.act("agent-a", "freeze", "stand down", actor="evans",
                                        actor_source="terminal"))
    # F-8: no verdict arrived, so the word is `timeout`; `failed` would mean it ran and broke.
    assert record.state == "timeout"
    assert "could not be read" in record.said and "still present" not in record.said
    # ... and it got there by polling to the deadline, not by settling on the first
    # failed read: an unreadable runtime is not a verdict (F-8's clock note).
    inspects = [call for call in runtime.calls if call[0] == "inspect"]
    assert len(inspects) >= 2


def test_an_unknown_verdict_word_is_refused_not_coerced(tmp_path: Path) -> None:
    # F-6: coerce is how a fourth word reaches the page.
    manager = _manager(tmp_path, RecordingRuntime())
    row = manager.store.create_action(action_id="a1", handle="agent-a", verb="freeze",
                                      actor="evans", reason="r", boot_id="b1", at=NOW())
    with pytest.raises(ValueError):
        manager._finish(row, "okay", "no", "FREEZE")  # noqa: SLF001 - the guard is the test


def test_the_decision_table_is_pure_and_names_the_handle(tmp_path: Path) -> None:
    assert decide("freeze", None, None, "agent-a").detail == "nothing to freeze: no pod for agent-a"
    assert decide("rescue", None, None, "agent-a").detail == "nothing to rescue: no pod for agent-a"
    assert decide("destroy", None, None, "a").detail == "nothing to destroy: no pod for a"
    assert decide("freeze", ContainerFacts(name="a", state="exited"), None, "a").detail == (
        "a is exited, not alive; nothing to freeze"
    )


def test_unknown_action_id_is_not_found_not_a_synthesised_failure(tmp_path: Path) -> None:
    manager = _manager(tmp_path, RecordingRuntime())
    with pytest.raises(UnknownActionError):
        manager.action("nope")


def test_the_manager_satisfies_the_office_interface(tmp_path: Path) -> None:
    from babo.office.api import OfficeManager

    assert isinstance(_manager(tmp_path, RecordingRuntime()), OfficeManager)
    assert isinstance(_manager(tmp_path, RecordingRuntime()).action, object)
    assert ActionRecord(id="x", handle="a", action="spawn").terminal is False


# -- L-10: `restart-ui`, the recorded sixth verb -------------------------
# The verb replaces the UI pod and thereby kills the sessions the old process
# held in memory. Its row's handle is the pod's name; the agents view never
# lists it (§8.1); and with the vocabulary six-wide no refusal may name the verb
# as both known and unknown (Bomu's acceptance (f)).

UI = "babo-ui"


def _ui_pod(state: str = "running") -> ContainerFacts:
    return ContainerFacts(name=UI, state=state, ip="10.0.0.11")


def _ui_spec() -> LaunchSpec:
    return LaunchSpec(name=UI, image="babo-ui:test")


def test_the_six_wide_vocabulary_never_names_a_verb_as_known_and_unknown(
    tmp_path: Path,
) -> None:
    # L-10 acceptance (f): the old sentence would have read "unknown action
    # 'restart-ui'; it is one of ..., restart-ui". Naming the verb once is the
    # whole assertion.
    manager = _manager(tmp_path, RecordingRuntime())
    with pytest.raises(CoreRefusalError) as caught:
        manager.act("agent-a", "explode", "because", actor="evans", actor_source="terminal")
    detail = str(caught.value)
    assert "restart-ui" in detail
    assert detail.count("restart-ui") == 1


def test_restart_ui_with_no_ui_pod_is_refused_and_names_the_pod(tmp_path: Path) -> None:
    # L-10: a restart of nothing is not a success.
    runtime = RecordingRuntime()
    manager = _manager(tmp_path, runtime, ui_spec=_ui_spec)
    record = manager.action(manager.act(UI, "restart-ui", "sessions leaked", actor="evans",
                                        actor_source="terminal"))
    assert record.state == "refused"
    assert record.said == "nothing to restart: no babo-ui pod"
    assert runtime.calls == [("inspect", UI)]


def test_restart_ui_on_a_pod_that_is_not_alive_names_the_state(tmp_path: Path) -> None:
    # A pod that is not alive is a spawn question: its sessions already died
    # with it, so there is nothing here to invalidate.
    runtime = RecordingRuntime(facts={UI: _ui_pod("exited")})
    manager = _manager(tmp_path, runtime, ui_spec=_ui_spec)
    record = manager.action(manager.act(UI, "restart-ui", "x", actor="evans",
                                        actor_source="terminal"))
    assert record.state == "refused"
    assert record.said == "nothing to restart: babo-ui is exited, not alive"
    assert ("remove", UI) not in runtime.calls


def test_restart_ui_replaces_the_pod_and_invalidates_by_construction(tmp_path: Path) -> None:
    # L-10 (b)/(c): the row's handle is the pod's name, the old pod is removed
    # before the new one runs, and the audit row names the actor and the reason.
    runtime = RecordingRuntime(facts={UI: _ui_pod()})
    manager = _manager(tmp_path, runtime, ui_spec=_ui_spec)
    record = manager.action(manager.act(UI, "restart-ui", "operator requested", actor="evans",
                                        actor_source="terminal"))
    assert record.state == "succeeded"
    assert record.handle == UI
    assert [call for call in runtime.calls if call[0] in ("remove", "run")] == [
        ("remove", UI), ("run", UI)
    ]
    assert "sessions minted before it died with the old process" in record.said
    audit = manager.store.audit(event="UI_RESTART")
    assert audit[0].handle == UI
    assert audit[0].actor == "evans"
    assert audit[0].reason == "operator requested"


def test_restart_ui_without_a_renderer_names_the_gap_and_leaves_the_ui_alone(
    tmp_path: Path,
) -> None:
    # The named gap, not a silent success: no renderer means the old pod must not
    # be removed, because the product would be left with no UI at all.
    runtime = RecordingRuntime(facts={UI: _ui_pod()})
    manager = _manager(tmp_path, runtime)  # no ui_spec
    record = manager.action(manager.act(UI, "restart-ui", "x", actor="evans",
                                        actor_source="terminal"))
    assert record.state == "failed"
    assert "ui_spec is not wired" in record.said
    assert ("remove", UI) not in runtime.calls


def test_a_direct_execute_cannot_spawn_a_second_pod(tmp_path: Path) -> None:
    # D12-10e2 (Bomu): `act()` is not the only road to `_execute`, so the
    # executor re-reads the world before the one call that makes a pod.
    runtime = RecordingRuntime(facts={"agent-a": _alive()})
    manager = _manager(tmp_path, runtime)
    row = manager.store.create_action(action_id="direct", handle="agent-a", verb="spawn",
                                      actor="evans", reason="r", boot_id="b1", at=NOW())
    state, said, _ = manager._execute(row, Identity("kimi", "project-x"))  # noqa: SLF001
    assert state == "succeeded"
    assert "existing pod" in said
    assert [call for call in runtime.calls if call[0] == "run"] == []


def test_the_ui_pod_is_never_listed_as_an_agent(tmp_path: Path) -> None:
    # §8.1 (d)/(e): the action table names what was acted on; the agents view
    # lists agents. The two facts coexist, which is why the row keeps the name.
    runtime = RecordingRuntime(facts={UI: _ui_pod(), "agent-a": _alive()})
    manager = _manager(tmp_path, runtime, ui_spec=_ui_spec)
    handles = [agent.handle for agent in manager.agents()]
    assert UI not in handles
    assert "agent-a" in handles
    action_id = manager.act(UI, "restart-ui", "x", actor="evans", actor_source="terminal")
    assert manager.action(action_id).handle == UI
