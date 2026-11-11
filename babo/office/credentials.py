"""The credential ledger: rows that make a secret findable, never rows that hold one.

The rules here are the frozen ledger's (C-01 … C-07, D-4), and they are
deliberately awkward in one place: the handle (a locator, not a token) is
derived from ``(instance, scope)`` **before** the mint, and the row is written
``pending`` before the external call. A crash between the two is recoverable
because the locator can be derived again. A store that cannot tell which locator
the adapter derived refuses to promote the row.

Nothing in this module writes a secret to disk. ``Minted.secret`` is returned to
the caller for delivery and is excluded from its own repr, so an accidental log
line prints the type, not the value.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from babo.office.audit import AuditRow, CredentialRecord, Store, StoreError

# Closed set, preserved verbatim from the model (C-01).
SCOPES = (
    "forgejo-user",
    "forgejo-token",
    "ssh-key",
    "valley-token",
    "vikunja-token",
    "generic-token",
)

# The four triggers, with their reason strings (C-03). No fifth reason.
REASONS = ("teardown", "orphan", "relaunch", "boot-reconcile")

# A field whose *name* looks like a value is refused at construction: the row
# has no column for one, and the API must not grow one by accident (C-01).
_VALUE_SHAPED = re.compile(r"(?i)(value|secret|password|passwd|private_key|key_material)")


class LedgerRefusalError(RuntimeError):
    """The ledger declined before minting. Detail names what made it impossible."""


class MintFailedError(RuntimeError):
    """The adapter's mint did not succeed. The row is `failed`, never `open` (D-4d)."""


@dataclass(frozen=True)
class Minted:
    """The transient result of a mint. The secret is not stored by this module."""

    secret: str = field(repr=False, compare=False)
    expires_at: str | None = None
    unrevocable: bool = False
    note: str = ""


@dataclass(frozen=True)
class RevokeOutcome:
    ok: bool
    note: str = ""
    skipped: bool = False  # F-3: nothing was attempted (never minted), not "revoked"


@dataclass(frozen=True)
class RevokeReport:
    revoked: int = 0
    failed: int = 0
    unrevocable: int = 0
    skipped: int = 0
    total: int = 0
    unreadable: bool = False

    def detail(self, trigger: str) -> str:
        return (
            f"{trigger}: revoked={self.revoked} failed={self.failed} "
            f"unrevocable={self.unrevocable} skipped={self.skipped} total={self.total}"
            + (" (runtime inventory unreadable; nothing revoked)" if self.unreadable else "")
        )


class Adapter(Protocol):
    """One service's three acts: derive a locator, mint, revoke.

    ``locator`` is called before ``mint`` and must be a pure function of
    ``(instance, scope)``. An adapter that cannot do that is not approved; the
    ledger refuses it at issue time rather than trusting it.
    """

    prefix: str

    def locator(self, instance: str, scope: str) -> str: ...

    def mint(self, instance: str, scope: str) -> Minted: ...

    def revoke(self, handle: str) -> RevokeOutcome: ...


class FileAdapter:
    """File-backed credentials: a real, testable revoker (C-05's proven path)."""

    prefix = "file:"

    def __init__(self, base: str | Path) -> None:
        self.base = Path(base)

    def locator(self, instance: str, scope: str) -> str:
        return f"file:{self.base / instance / (scope + '.key')}"

    def mint(self, instance: str, scope: str) -> Minted:
        target = Path(self.locator(instance, scope)[len("file:"):])
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
        target.chmod(0o600)
        return Minted(secret=target.read_text(encoding="utf-8").strip())

    def revoke(self, handle: str) -> RevokeOutcome:
        path = Path(handle[len("file:"):]) if handle.startswith("file:") else Path(handle)
        try:
            path.unlink()
        except FileNotFoundError:
            return RevokeOutcome(True, "already gone")
        except OSError as exc:
            return RevokeOutcome(False, f"{type(exc).__name__}: {exc}")
        if path.exists():
            return RevokeOutcome(False, "file still present after removal")
        return RevokeOutcome(True, "removed")


class TokenAdapter:
    """A handle-prefixed network token with injected calls (the faux-HTTP path).

    The mint and revoke callables are injected so the ledger can be tested
    without a live service. That is also why C-05's caveat travels: live
    revocation of forgejo/valley/vikunja tokens has only ever been proven
    against a fake client.
    """

    def __init__(self, prefix: str, locator: Callable[[str, str], str],
                 mint: Callable[[str, str], Minted],
                 revoke: Callable[[str], RevokeOutcome]) -> None:
        self.prefix = prefix
        self._locator = locator
        self._mint = mint
        self._revoke = revoke

    def locator(self, instance: str, scope: str) -> str:
        return self._locator(instance, scope)

    def mint(self, instance: str, scope: str) -> Minted:
        return self._mint(instance, scope)

    def revoke(self, handle: str) -> RevokeOutcome:
        return self._revoke(handle)


class AdapterRegistry:
    """The five handle prefixes, plus one default that refuses (C-04)."""

    PREFIXES = ("file:", "forgejo:user:", "valley:token:", "vikunja:token:", "generic:")

    def __init__(self) -> None:
        self._by_prefix: dict[str, Adapter] = {}

    def register(self, adapter: Adapter) -> None:
        if adapter.prefix not in self.PREFIXES:
            raise LedgerRefusalError(
                f"adapter prefix {adapter.prefix!r} is not one of the five approved handles"
            )
        # D-4(a): an adapter that cannot derive a deterministic locator is not
        # approved. Two calls must agree, byte for byte, before it is trusted.
        probe = adapter.locator("probe-instance", "ssh-key")
        again = adapter.locator("probe-instance", "ssh-key")
        if not probe or probe != again:
            raise LedgerRefusalError(
                f"adapter {adapter.prefix!r} cannot derive a deterministic locator from "
                "(instance, scope); it is not approved"
            )
        if _VALUE_SHAPED.search(probe):
            raise LedgerRefusalError(f"adapter {adapter.prefix!r} derives a value-shaped locator")
        self._by_prefix[adapter.prefix] = adapter

    def for_handle(self, handle: str) -> Adapter | None:
        for prefix in sorted(self._by_prefix, key=len, reverse=True):
            if handle.startswith(prefix):
                return self._by_prefix[prefix]
        return None


class CredentialLedger:
    def __init__(self, store: Store, *, registry: AdapterRegistry | None = None,
                 clock: Callable[[], str]) -> None:
        self.store = store
        self.registry = registry or AdapterRegistry()
        self._clock = clock

    # -- issue (D-4) ----------------------------------------------------
    def issue(self, adapter: Adapter, instance: str, scope: str, *, boot_id: str,
              launch_ref: str, expires_at: str | None = None, **extra: object
              ) -> tuple[CredentialRecord, Minted]:
        for name in extra:
            if _VALUE_SHAPED.search(name):
                raise LedgerRefusalError(
                    f"refusing an issue request carrying {name!r}: the ledger has no value field"
                )
            raise LedgerRefusalError(f"unknown issue field {name!r}")
        if scope not in SCOPES:
            raise LedgerRefusalError(f"unknown credential scope {scope!r}; it is one of {', '.join(SCOPES)}")
        if adapter.prefix not in AdapterRegistry.PREFIXES:
            raise LedgerRefusalError(f"unapproved adapter {adapter.prefix!r}")

        # The locator is derived BEFORE the mint and is the only thing the row
        # records about the secret's address.
        locator = adapter.locator(instance, scope)
        row = CredentialRecord(
            id=secrets.token_hex(16), instance=instance, scope=scope, boot_id=boot_id,
            launch_ref=launch_ref, locator=locator, issued_at=self._clock(), expires_at=expires_at,
            state="pending", unrevocable=False,
        )
        try:
            self.store.insert_credential(row)
        except StoreError as exc:  # collision: not deterministic per (instance, scope)
            raise LedgerRefusalError(str(exc)) from exc

        try:
            minted = adapter.mint(instance, scope)
        except Exception as exc:
            # D-4(d): no secret is assumed to exist unless the adapter confirms it.
            self.store.fail_credential(row.id, note=f"{type(exc).__name__}: {exc}"[:300])
            raise MintFailedError(f"{type(exc).__name__}: {exc}") from exc

        if adapter.locator(instance, scope) != locator:  # pragma: no cover - race or a lying adapter
            self.store.fail_credential(row.id, note="adapter changed its locator between derive and mint")
            raise LedgerRefusalError("adapter changed its locator between derive and mint")

        promoted = self.store.promote_credential(row.id, locator=locator, at=self._clock())
        if minted.unrevocable:
            # C-02: a credential nobody can close is recorded as such and stays open.
            promoted = self.store.mark_unrevocable(
                promoted.id, note=minted.note or "no revoke endpoint configured"
            )
        return self.store.get_credential(promoted.id), minted  # type: ignore[return-value]

    # -- revoke (C-02 … C-05) -------------------------------------------
    def revoke(self, credential_id: str, reason: str) -> RevokeOutcome:
        if reason not in REASONS:
            raise LedgerRefusalError(f"unknown revoke reason {reason!r}; it is one of {', '.join(REASONS)}")
        row = self.store.get_credential(credential_id)
        if row is None:
            return RevokeOutcome(False, f"no such credential: {credential_id}")
        if row.state == "revoked":
            return RevokeOutcome(True, "already revoked")
        if row.state == "failed":
            return RevokeOutcome(True, "mint never succeeded; nothing to revoke", skipped=True)
        if row.unrevocable:
            return RevokeOutcome(False, "unrevocable")
        adapter = self.registry.for_handle(row.locator)
        if adapter is None:
            note = "no revoker for handle kind"
            self.store.note_credential(row.id, note=note)
            return RevokeOutcome(False, note)
        try:
            outcome = adapter.revoke(row.locator)
        except Exception as exc:  # per-row failure never aborts the sweep
            note = f"{type(exc).__name__}: {exc}"[:300]
            self.store.note_credential(row.id, note=note)
            return RevokeOutcome(False, note)
        if not outcome.ok:
            self.store.note_credential(row.id, note=outcome.note or "revocation not confirmed")
            return RevokeOutcome(False, outcome.note or "revocation not confirmed")
        self.store.revoke_credential(row.id, at=self._clock(), reason=reason,
                                     note=outcome.note)
        return RevokeOutcome(True, outcome.note)

    def revoke_all(self, instance: str, reason: str) -> RevokeReport:
        rows = [r for r in self.store.credentials(instance=instance) if r.state in ("open", "pending")]
        revoked = failed = unrevocable = skipped = 0
        for row in rows:
            if row.unrevocable:
                unrevocable += 1
                continue
            outcome = self.revoke(row.id, reason)
            if outcome.skipped:
                skipped += 1
            elif outcome.ok:
                revoked += 1
            else:
                failed += 1
        report = RevokeReport(revoked=revoked, failed=failed, unrevocable=unrevocable,
                              skipped=skipped, total=len(rows))
        if failed:
            self.store.insert_audit(AuditRow(
                event="CREDENTIAL_REVOKE_PARTIAL", handle=instance, at=self._clock(),
                detail=report.detail(reason), state="failed",
            ))
        return report

    # -- reconciliation (C-03, C-06) ------------------------------------
    def reconcile_boot(self, *, boot_id: str, live_instances: set[str],
                       runtime_readable: bool = True) -> RevokeReport:
        """Conservative in both directions: current boot and live pods are left alone."""
        if not runtime_readable:
            rows = [r for r in self.store.credentials() if r.state in ("open", "pending")]
            report = RevokeReport(skipped=len(rows), total=len(rows), unreadable=True)
            self.store.insert_audit(AuditRow(
                event="CREDENTIAL_RECONCILE", handle="*", at=self._clock(),
                detail=report.detail("boot-reconcile"), state="unknown",
            ))
            return report
        rows = [r for r in self.store.credentials() if r.state in ("open", "pending")]
        revoked = failed = unrevocable = skipped = 0
        for row in rows:
            if row.boot_id == boot_id or row.instance in live_instances:
                skipped += 1
                continue
            if row.unrevocable:
                unrevocable += 1
                continue
            outcome = self.revoke(row.id, "boot-reconcile")
            if outcome.skipped:
                skipped += 1
            elif outcome.ok:
                revoked += 1
            else:
                failed += 1
        report = RevokeReport(revoked=revoked, failed=failed, unrevocable=unrevocable,
                              skipped=skipped, total=len(rows))
        self.store.insert_audit(AuditRow(
            event="CREDENTIAL_RECONCILE", handle="*", at=self._clock(),
            detail=report.detail("boot-reconcile"), state="failed" if failed else "succeeded",
        ))
        return report

    def revoke_orphans(self, *, live_instances: set[str]) -> RevokeReport:
        """Open rows whose instance has no pod: orphan cleanup, its own reason (C-03)."""
        rows = [r for r in self.store.credentials() if r.state in ("open", "pending")
                and r.instance not in live_instances]
        revoked = failed = unrevocable = 0
        for row in rows:
            if row.unrevocable:
                unrevocable += 1
                continue
            outcome = self.revoke(row.id, "orphan")
            if outcome.skipped:
                continue
            if outcome.ok:
                revoked += 1
            else:
                failed += 1
        report = RevokeReport(revoked=revoked, failed=failed, unrevocable=unrevocable,
                              total=len(rows))
        if failed:
            self.store.insert_audit(AuditRow(
                event="CREDENTIAL_REVOKE_PARTIAL", handle="*", at=self._clock(),
                detail=report.detail("orphan"), state="failed",
            ))
        return report
