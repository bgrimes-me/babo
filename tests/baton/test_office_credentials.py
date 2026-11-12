"""The credential ledger's honesty rules (C-01 … C-07, D-4), proven locally."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from babo.office.audit import CredentialRecord, Store, StoreError
from babo.office.credentials import (
    AdapterRegistry,
    CredentialLedger,
    FileAdapter,
    LedgerRefusalError,
    Minted,
    MintFailedError,
    RevokeOutcome,
    TokenAdapter,
)

NOW = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")  # noqa: E731


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "m.db")


def _ledger(tmp_path: Path, store: Store | None = None) -> tuple[CredentialLedger, FileAdapter]:
    ledger = CredentialLedger(store or _store(tmp_path), clock=NOW)
    adapter = FileAdapter(tmp_path / "creds")
    ledger.registry.register(adapter)
    return ledger, adapter


def test_the_locator_is_deterministic_and_secret_free(tmp_path: Path) -> None:
    # D-4 acceptance (a): two calls, same (instance, scope), byte-identical handles.
    _ledger_obj, adapter = _ledger(tmp_path)
    first = adapter.locator("agent-a", "ssh-key")
    again = adapter.locator("agent-a", "ssh-key")
    assert first == again
    assert first.startswith("file:")
    assert not re.search(r"(?i)(secret|password|value=)", first)
    assert adapter.locator("agent-b", "ssh-key") != first


def test_an_adapter_that_cannot_derive_a_deterministic_locator_is_not_approved(
    tmp_path: Path,
) -> None:
    # D-4(a): refused at registration, naming the adapter; the type cannot be used.
    registry = AdapterRegistry()
    calls = {"n": 0}

    def unstable(instance: str, scope: str) -> str:
        calls["n"] += 1
        return f"valley:token:{instance}:{scope}:{calls['n']}"

    flaky = TokenAdapter("valley:token:", unstable, lambda i, s: Minted(secret="x"),
                         lambda h: RevokeOutcome(True))
    with pytest.raises(LedgerRefusalError) as caught:
        registry.register(flaky)
    assert "cannot derive a deterministic locator" in str(caught.value)


def test_pending_row_promotes_on_mint_success_and_holds_no_secret(tmp_path: Path) -> None:
    # D-4 acceptance (b) + C-01: one row, no value, no value-shaped column.
    ledger, adapter = _ledger(tmp_path)
    row, minted = ledger.issue(adapter, "agent-a", "ssh-key", boot_id="b1", launch_ref="b1:agent-a")
    assert row.state == "open"
    assert row.locator == adapter.locator("agent-a", "ssh-key")
    stored = ledger.store.get_credential(row.id)
    assert stored is not None and stored.state == "open"
    for field in (stored.id, stored.instance, stored.scope, stored.locator, stored.note):
        assert minted.secret not in field
    assert minted.secret not in repr(minted)  # the transient value is not printable by accident


def test_the_store_refuses_to_promote_a_locator_the_adapter_did_not_derive(
    tmp_path: Path,
) -> None:
    # This is the refusal soro said he would check hardest: a rule, not a comment.
    store = _store(tmp_path)
    adapter = FileAdapter(tmp_path / "creds")
    record = CredentialRecord(id="c1", instance="agent-a", scope="ssh-key", boot_id="b1",
                              launch_ref="b1:agent-a", locator=adapter.locator("agent-a", "ssh-key"),
                              issued_at=NOW(), expires_at=None, state="pending", unrevocable=False)
    store.insert_credential(record)
    with pytest.raises(StoreError) as caught:
        store.promote_credential("c1", locator="file:/somewhere/else.key", at=NOW())
    assert "locator the adapter did not derive" in str(caught.value)


def test_mint_failure_moves_the_row_to_failed_never_open(tmp_path: Path) -> None:
    # D-4(d): no secret is assumed to exist unless the adapter confirms it.
    ledger, _adapter = _ledger(tmp_path)
    exploding = TokenAdapter("generic:", lambda i, s: f"generic:{i}/{s}",
                             lambda i, s: (_ for _ in ()).throw(RuntimeError("mint refused")),
                             lambda h: RevokeOutcome(True))
    ledger.registry.register(exploding)
    with pytest.raises(MintFailedError):
        ledger.issue(exploding, "agent-a", "generic-token", boot_id="b1", launch_ref="b1:agent-a")
    (row,) = ledger.store.credentials(instance="agent-a")
    assert row.state == "failed"
    assert "mint refused" in row.note


def test_a_crash_between_mint_and_promotion_is_reconciled_by_locator(tmp_path: Path) -> None:
    # D-4 acceptance (c): the locator survives the crash a row cannot.
    store = _store(tmp_path)
    adapter = FileAdapter(tmp_path / "creds")
    locator = adapter.locator("agent-a", "ssh-key")
    minted = adapter.mint("agent-a", "ssh-key")  # the secret exists; the row is still pending
    store.insert_credential(CredentialRecord(
        id="c-crash", instance="agent-a", scope="ssh-key", boot_id="b1",
        launch_ref="b1:agent-a", locator=locator, issued_at=NOW(), expires_at=None,
        state="pending", unrevocable=False,
    ))
    ledger = CredentialLedger(store, clock=NOW)
    ledger.registry.register(adapter)
    report = ledger.reconcile_boot(boot_id="b2", live_instances=set(), runtime_readable=True)
    assert report.revoked == 1
    assert store.get_credential("c-crash").state == "revoked"
    assert store.get_credential("c-crash").revoked_reason == "boot-reconcile"
    assert not Path(locator[len("file:"):]).exists()
    assert minted.secret  # the value existed; it just never reached the row


def test_an_unrevocable_row_stays_open_and_is_counted_as_such(tmp_path: Path) -> None:
    # C-02: "we closed everything" is never said about a credential nobody can close.
    ledger, _adapter = _ledger(tmp_path)
    generic = TokenAdapter("generic:", lambda i, s: f"generic:{i}/{s}",
                           lambda i, s: Minted(secret="x", unrevocable=True,
                                               note="no revoke endpoint configured"),
                           lambda h: RevokeOutcome(False, "no revoke endpoint configured"))
    ledger.registry.register(generic)
    row, _ = ledger.issue(generic, "agent-a", "generic-token", boot_id="b1", launch_ref="b1:a")
    report = ledger.revoke_all("agent-a", "teardown")
    assert report.unrevocable == 1 and report.revoked == 0
    assert ledger.store.get_credential(row.id).state == "open"


def test_a_failed_revoke_leaves_the_row_open_and_the_sweep_continues(tmp_path: Path) -> None:
    # C-05: never mark a row that was not actually revoked; one row never aborts the rest.
    ledger, _adapter = _ledger(tmp_path)
    ledger.registry.register(TokenAdapter(
        "generic:", lambda i, s: f"generic:{i}/{s}", lambda i, s: Minted(secret="x"),
        lambda h: RevokeOutcome(False, "HTTP 500"),
    ))
    generic = ledger.registry.for_handle("generic:x/y")
    row, _ = ledger.issue(generic, "agent-a", "generic-token", boot_id="b1", launch_ref="b1:a")
    report = ledger.revoke_all("agent-a", "teardown")
    assert report.failed == 1 and report.revoked == 0
    row = ledger.store.get_credential(row.id)
    assert row.state == "open" and "HTTP 500" in row.note


def test_an_unknown_handle_prefix_is_failed_and_named(tmp_path: Path) -> None:
    # C-04: a handle kind with no revoker is annotated and counted failed.
    store = _store(tmp_path)
    ledger = CredentialLedger(store, clock=NOW)
    store.insert_credential(CredentialRecord(
        id="c-unknown", instance="agent-a", scope="ssh-key", boot_id="b1", launch_ref="b1:a",
        locator="mystery:opaque", issued_at=NOW(), expires_at=None, state="open",
        unrevocable=False,
    ))
    report = ledger.revoke_all("agent-a", "teardown")
    assert report.failed == 1
    row = store.get_credential("c-unknown")
    assert row.state == "open" and row.note == "no revoker for handle kind"


def test_the_five_handle_prefixes_are_recognised_verbatim() -> None:
    # C-04 / oracle comparison: the strings must match the model's rows exactly.
    registry = AdapterRegistry()
    for prefix in ("file:", "forgejo:user:", "valley:token:", "vikunja:token:", "generic:"):
        registry.register(TokenAdapter(prefix, lambda i, s: f"{prefix}{i}/{s}",
                                       lambda i, s: Minted(secret="x"),
                                       lambda h: RevokeOutcome(True)))
    for handle, prefix in (
        ("file:/tmp/k", "file:"),
        ("forgejo:user:kimi", "forgejo:user:"),
        ("valley:token:kimi", "valley:token:"),
        ("vikunja:token:kimi", "vikunja:token:"),
        ("generic:https://x.test", "generic:"),
    ):
        assert registry.for_handle(handle) is not None
        assert registry.for_handle(handle).prefix == prefix  # type: ignore[union-attr]
    assert registry.for_handle("ftp:nope") is None


def test_boot_reconciliation_is_conservative_in_both_directions(tmp_path: Path) -> None:
    # C-06: current boot and live pods are left alone; old orphans are revoked.
    store = _store(tmp_path)
    adapter = FileAdapter(tmp_path / "creds")
    ledger = CredentialLedger(store, clock=NOW)
    ledger.registry.register(adapter)
    old, _ = ledger.issue(adapter, "orphan-a", "ssh-key", boot_id="b1", launch_ref="b1:orphan-a")
    live, _ = ledger.issue(adapter, "live-a", "ssh-key", boot_id="b1", launch_ref="b1:live-a")
    current, _ = ledger.issue(adapter, "new-a", "ssh-key", boot_id="b2", launch_ref="b2:new-a")
    report = ledger.reconcile_boot(boot_id="b2", live_instances={"live-a"}, runtime_readable=True)
    assert report.revoked == 1 and report.skipped == 2
    assert store.get_credential(old.id).state == "revoked"
    assert store.get_credential(live.id).state == "open"
    assert store.get_credential(current.id).state == "open"


def test_an_unreadable_inventory_revokes_nothing_and_says_so(tmp_path: Path) -> None:
    # C-06 / O-05: unreadable runtime → no revocation at all, counts recorded.
    ledger, adapter = _ledger(tmp_path)
    row, _ = ledger.issue(adapter, "agent-a", "ssh-key", boot_id="b1", launch_ref="b1:a")
    report = ledger.reconcile_boot(boot_id="b2", live_instances=set(), runtime_readable=False)
    assert report.unreadable is True and report.revoked == 0 and report.skipped == 1
    assert ledger.store.get_credential(row.id).state == "open"
    audit = ledger.store.audit(event="CREDENTIAL_RECONCILE")
    assert "nothing revoked" in audit[0].detail


def test_a_scope_ignoring_adapter_is_caught_by_the_locator_index(tmp_path: Path) -> None:
    # D-4(c) with the guard moved to the locator: an adapter that ignores scope
    # passes the determinism probe (same scope twice is stable) and is still
    # refused, because the *index* on live locators is what catches it.
    ledger, _adapter = _ledger(tmp_path)
    scopeless = TokenAdapter(
        "generic:",
        lambda instance, scope: f"generic:{instance}",  # scope ignored on purpose
        lambda i, s: Minted(secret="x"),
        lambda h: RevokeOutcome(True),
    )
    ledger.registry.register(scopeless)  # registration alone cannot see this defect
    ledger.issue(scopeless, "agent-a", "ssh-key", boot_id="b1", launch_ref="b1:a")
    with pytest.raises(LedgerRefusalError) as caught:
        ledger.issue(scopeless, "agent-a", "generic-token", boot_id="b1", launch_ref="b1:a")
    assert "locator" in str(caught.value)
    assert len(ledger.store.credentials(instance="agent-a")) == 1

    # The index itself, without the ledger in the way: two live rows with the
    # same locator and different (instance, scope) are refused by the store.
    store = _store(tmp_path / "direct")
    shared = "generic:collision"
    store.insert_credential(CredentialRecord(
        id="c1", instance="agent-b", scope="ssh-key", boot_id="b1", launch_ref="b1:b",
        locator=shared, issued_at=NOW(), expires_at=None, state="open", unrevocable=False,
    ))
    with pytest.raises(StoreError):
        store.insert_credential(CredentialRecord(
            id="c2", instance="agent-c", scope="generic-token", boot_id="b1", launch_ref="b1:c",
            locator=shared, issued_at=NOW(), expires_at=None, state="open", unrevocable=False,
        ))


def test_a_second_live_row_for_the_same_instance_and_scope_is_refused(tmp_path: Path) -> None:
    # D-4(c): a collision proves the locator is not deterministic per (instance, scope).
    ledger, adapter = _ledger(tmp_path)
    ledger.issue(adapter, "agent-a", "ssh-key", boot_id="b1", launch_ref="b1:a")
    with pytest.raises(LedgerRefusalError) as caught:
        ledger.issue(adapter, "agent-a", "ssh-key", boot_id="b1", launch_ref="b1:a")
    assert "live credential already holds this locator" in str(caught.value)


def test_an_issue_request_carrying_a_value_shaped_field_is_refused(tmp_path: Path) -> None:
    # C-01: the row has no place for a secret, and the API must not grow one.
    ledger, adapter = _ledger(tmp_path)
    with pytest.raises(LedgerRefusalError) as caught:
        ledger.issue(adapter, "agent-a", "ssh-key", boot_id="b1", launch_ref="b1:a", secret="x")
    assert "value field" in str(caught.value)
    assert ledger.store.credentials(instance="agent-a") == []


def test_unknown_scopes_and_reasons_are_refused_with_the_allowed_set(tmp_path: Path) -> None:
    ledger, adapter = _ledger(tmp_path)
    with pytest.raises(LedgerRefusalError) as scope:
        ledger.issue(adapter, "agent-a", "admin", boot_id="b1", launch_ref="b1:a")
    assert "forgejo-user" in str(scope.value)
    row, _ = ledger.issue(adapter, "agent-a", "ssh-key", boot_id="b1", launch_ref="b1:a")
    with pytest.raises(LedgerRefusalError) as reason:
        ledger.revoke(row.id, "because")
    assert "boot-reconcile" in str(reason.value)


def test_a_never_minted_row_is_skipped_not_counted_as_revoked(tmp_path: Path) -> None:
    # F-3: nothing was attempted, so nothing was revoked; the skipped counter says so.
    ledger, _adapter = _ledger(tmp_path)
    exploding = TokenAdapter("generic:", lambda i, s: f"generic:{i}/{s}",
                             lambda i, s: (_ for _ in ()).throw(RuntimeError("no mint")),
                             lambda h: RevokeOutcome(True))
    ledger.registry.register(exploding)
    with pytest.raises(MintFailedError):
        ledger.issue(exploding, "agent-a", "generic-token", boot_id="b1", launch_ref="b1:a")
    (row,) = ledger.store.credentials(instance="agent-a")
    outcome = ledger.revoke(row.id, "teardown")
    assert outcome.skipped is True and outcome.ok is True
    report = ledger.revoke_all("agent-a", "teardown")
    assert (report.revoked, report.failed, report.skipped) == (0, 0, 0)  # already failed: not selected


def test_a_pending_row_takes_the_revoke_failure_note(tmp_path: Path) -> None:
    # F-4: D-4(b)'s sweeper path must not lose the reason on a pending row.
    store = _store(tmp_path)
    ledger = CredentialLedger(store, clock=NOW)
    store.insert_credential(CredentialRecord(
        id="c-pending", instance="agent-a", scope="ssh-key", boot_id="b1", launch_ref="b1:a",
        locator="mystery:opaque", issued_at=NOW(), expires_at=None, state="pending",
        unrevocable=False,
    ))
    report = ledger.revoke_all("agent-a", "boot-reconcile")
    assert report.failed == 1
    row = store.get_credential("c-pending")
    assert row.state == "pending" and row.note == "no revoker for handle kind"


def test_unrevocable_cannot_be_marked_on_a_closed_row(tmp_path: Path) -> None:
    # F-5: the flag means "open on purpose", not "annotate history".
    store = _store(tmp_path)
    store.insert_credential(CredentialRecord(
        id="c-revoked", instance="agent-a", scope="ssh-key", boot_id="b1", launch_ref="b1:a",
        locator="file:/tmp/x", issued_at=NOW(), expires_at=None, state="revoked",
        unrevocable=False,
    ))
    with pytest.raises(StoreError):
        store.mark_unrevocable("c-revoked", note="too late")
