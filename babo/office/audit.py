"""Durable action and audit records for the native manager core.

Two tables, two different promises (ledger U-01, R-03, R-08):

- ``actions`` is the record of *requests*: one row per action, moving through
  the closed state set and settling exactly once. It is mutable only along the
  legal transitions the core applies.
- ``audit`` is append-only: rows are inserted, never updated, never deleted.
  The promise is enforced by the store (triggers plus an API with no mutation
  method), not by a comment — U-01's D-9.

An unreadable store is a load error, not an empty answer. The office must look
broken, not clean, when its record cannot be read.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from babo.office.api import TERMINAL

# The store's state set (ledger L-02). The terminal words are the interface's,
# not a second copy: api.py is the single source (Bomu's slice lint).
ACTION_STATES = ("pending", "running", *TERMINAL)

# The preserved event vocabulary, plus REFUSED: the model had no actor gate and
# so no audit row for a refusal; A-03/A-05 require one. Named here, not invented
# per call site.
EVENTS = (
    "SPAWN",
    "SPAWN_FAILED",
    "FREEZE",
    "FREEZE_FALLBACK_RESCUE",
    "RESCUE",
    "TEARDOWN",
    "TEARDOWN_PARTIAL",
    "UI_RESTART",
    "UI_RESTART_FAILED",
    "CREDENTIAL_REVOKE_PARTIAL",
    "CREDENTIAL_RECONCILE",
    "REFUSED",
)

CREDENTIAL_STATES = ("pending", "open", "revoked", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id           TEXT PRIMARY KEY,
    handle       TEXT NOT NULL,
    verb         TEXT NOT NULL,
    state        TEXT NOT NULL,
    said         TEXT NOT NULL DEFAULT '',
    actor        TEXT NOT NULL,
    actor_source TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL,
    boot_id      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS actions_by_handle ON actions (handle, verb, state);

CREATE TABLE IF NOT EXISTS audit (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    event        TEXT NOT NULL,
    handle       TEXT NOT NULL,
    identity     TEXT NOT NULL DEFAULT '',
    actor        TEXT NOT NULL DEFAULT '',
    actor_source TEXT NOT NULL DEFAULT '',
    verb         TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL DEFAULT '',
    state        TEXT NOT NULL DEFAULT '',
    action_id    TEXT,
    detail       TEXT NOT NULL DEFAULT ''
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN
    SELECT RAISE(ABORT, 'audit rows are append-only');
END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN
    SELECT RAISE(ABORT, 'audit rows are append-only');
END;

CREATE TABLE IF NOT EXISTS single_flight (
    handle     TEXT NOT NULL,
    verb       TEXT NOT NULL,
    action_id  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (handle, verb)
);

CREATE TABLE IF NOT EXISTS launches (
    handle   TEXT PRIMARY KEY,
    boot_id  TEXT NOT NULL,
    identity TEXT NOT NULL DEFAULT '',
    state    TEXT NOT NULL,
    ending   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS credentials (
    id             TEXT PRIMARY KEY,
    instance       TEXT NOT NULL,
    scope          TEXT NOT NULL,
    boot_id        TEXT NOT NULL,
    launch_ref     TEXT NOT NULL,
    locator        TEXT NOT NULL,
    issued_at      TEXT NOT NULL,
    expires_at     TEXT,
    state          TEXT NOT NULL,
    unrevocable    INTEGER NOT NULL DEFAULT 0,
    revoked_at     TEXT,
    revoked_reason TEXT,
    note           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS credentials_by_instance ON credentials (instance, state);
CREATE UNIQUE INDEX IF NOT EXISTS credentials_one_live_locator ON credentials (locator)
    WHERE state IN ('pending', 'open');
"""


class StoreError(RuntimeError):
    """The store refused a write that would have broken one of its rules."""


class AppendOnlyError(StoreError):
    """Something tried to change or remove an audit row."""


@dataclass(frozen=True)
class ActionRow:
    id: str
    handle: str
    verb: str
    state: str
    said: str
    actor: str
    reason: str
    boot_id: str
    created_at: str
    actor_source: str = ""
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL


@dataclass(frozen=True)
class AuditRow:
    event: str
    handle: str
    at: str
    identity: str = ""
    actor: str = ""
    actor_source: str = ""
    verb: str = ""
    reason: str = ""
    state: str = ""
    action_id: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class CredentialRecord:
    id: str
    instance: str
    scope: str
    boot_id: str
    launch_ref: str
    locator: str
    issued_at: str
    expires_at: str | None
    state: str
    unrevocable: bool
    revoked_at: str | None = None
    revoked_reason: str | None = None
    note: str = ""


class Store:
    """One SQLite file, one lock, explicit transactions.

    The action row, its audit row and the single-flight release are written in
    the same transaction (R-08); a reader never sees a settled action without
    the audit row that explains it.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            # Migration: the live-row uniqueness moved from (instance, scope) to
            # the locator itself. The locator is the thing the store can verify
            # was derived; a scope-ignoring adapter must collide here, not slip by.
            self._conn.execute("DROP INDEX IF EXISTS credentials_one_live")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS credentials_one_live_locator ON credentials"
                " (locator) WHERE state IN ('pending', 'open')"
            )
            self._add_column_if_missing("actions", "actor_source", "TEXT NOT NULL DEFAULT ''")
            self._add_column_if_missing("audit", "actor_source", "TEXT NOT NULL DEFAULT ''")

    def _add_column_if_missing(self, table: str, column: str, declaration: str) -> None:
        """A store written before F-7 still opens; provenance is added, not assumed."""
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        if column not in {row["name"] for row in rows}:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    # -- transactions ---------------------------------------------------
    def _begin(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._conn.execute("COMMIT")

    def _rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - only when no txn is open
            pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- actions --------------------------------------------------------
    def create_action(
        self,
        *,
        action_id: str,
        handle: str,
        verb: str,
        actor: str,
        reason: str,
        boot_id: str,
        at: str,
        said: str = "",
        state: str = "pending",
        actor_source: str = "",
    ) -> ActionRow:
        if state not in ACTION_STATES:
            raise StoreError(f"unknown action state {state!r}")
        with self._lock:
            try:
                self._begin()
                self._conn.execute(
                    "INSERT INTO actions (id, handle, verb, state, said, actor, actor_source, reason,"
                    " boot_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (action_id, handle, verb, state, said, actor, actor_source, reason, boot_id, at),
                )
                self._commit()
            except sqlite3.IntegrityError as exc:
                self._rollback()
                raise StoreError(f"action id already exists: {action_id}") from exc
            except Exception:
                self._rollback()
                raise
        return self.get_action(action_id)  # type: ignore[return-value]

    def get_action(self, action_id: str) -> ActionRow | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        return _action(row) if row else None

    def actions(
        self, *, handle: str | None = None, verbs: Iterable[str] | None = None, limit: int = 200
    ) -> list[ActionRow]:
        sql = "SELECT * FROM actions"
        clauses: list[str] = []
        args: list[Any] = []
        if handle:
            clauses.append("handle = ?")
            args.append(handle)
        if verbs:
            verbs = tuple(verbs)
            clauses.append("verb IN (" + ",".join("?" for _ in verbs) + ")")
            args.extend(verbs)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_action(row) for row in rows]

    def last_action(self, handle: str) -> ActionRow | None:
        rows = self.actions(handle=handle, limit=1)
        return rows[0] if rows else None

    def start_action(self, action_id: str, at: str) -> bool:
        """pending -> running. False when the row was already final (a replay won)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE actions SET state = 'running', started_at = ? WHERE id = ? AND state = 'pending'",
                (at, action_id),
            )
            return cur.rowcount == 1

    def finish_action(
        self,
        action_id: str,
        *,
        state: str,
        said: str,
        at: str,
        event: str,
        detail: str = "",
        identity: str = "",
    ) -> ActionRow:
        """Settle one action and write its audit row in the same transaction."""
        if state not in TERMINAL:
            raise StoreError(f"not a terminal state: {state!r}")
        with self._lock:
            row = self.get_action(action_id)
            if row is None:
                raise StoreError(f"no such action: {action_id}")
            if row.terminal:  # replay: nothing changes, including the record
                return row
            try:
                self._begin()
                self._conn.execute(
                    "UPDATE actions SET state = ?, said = ?, finished_at = ? WHERE id = ?",
                    (state, said, at, action_id),
                )
                self._conn.execute(
                    "INSERT INTO audit (at, event, handle, identity, actor, actor_source, verb,"
                    " reason, state, action_id, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (at, event, row.handle, identity, row.actor, row.actor_source, row.verb,
                     row.reason, state, action_id, detail or said),
                )
                self._conn.execute(
                    "DELETE FROM single_flight WHERE handle = ? AND verb = ?",
                    (row.handle, row.verb),
                )
                self._commit()
            except Exception:
                self._rollback()
                raise
        return self.get_action(action_id)  # type: ignore[return-value]

    # -- single flight (L-07 / D-3) --------------------------------------
    def claim(self, handle: str, verb: str, action_id: str, expires_at: str) -> None:
        with self._lock:
            try:
                self._begin()
                self._conn.execute(
                    "INSERT INTO single_flight (handle, verb, action_id, expires_at) VALUES (?, ?, ?, ?)",
                    (handle, verb, action_id, expires_at),
                )
                self._commit()
            except sqlite3.IntegrityError as exc:
                self._rollback()
                raise StoreError(f"an action is already in flight for ({handle}, {verb})") from exc
            except Exception:
                self._rollback()
                raise

    def claim_or_read(self, handle: str, verb: str, action_id: str, expires_at: str,
                      now: str) -> str:
        """Return ``action_id`` when the claim was taken, or the in-flight id when not.

        An expired row is not an action in flight; it is removed and the claim
        is retried once. Nothing here guesses: the expiry is the deadline the
        core wrote with the row.
        """
        with self._lock:
            try:
                self._begin()
                self._conn.execute(
                    "DELETE FROM single_flight WHERE handle = ? AND verb = ? AND expires_at <= ?",
                    (handle, verb, now),
                )
                self._conn.execute(
                    "INSERT INTO single_flight (handle, verb, action_id, expires_at) VALUES (?, ?, ?, ?)",
                    (handle, verb, action_id, expires_at),
                )
                self._commit()
                return action_id
            except sqlite3.IntegrityError as exc:
                self._rollback()
                existing = self._conn.execute(
                    "SELECT action_id FROM single_flight WHERE handle = ? AND verb = ?",
                    (handle, verb),
                ).fetchone()
                if existing is None:  # pragma: no cover - the row vanished between statements
                    raise StoreError("single-flight claim lost") from exc
                return str(existing["action_id"])
            except Exception:
                self._rollback()
                raise

    def release(self, handle: str, verb: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM single_flight WHERE handle = ? AND verb = ?", (handle, verb)
            )

    def in_flight(self, handle: str, verb: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT action_id FROM single_flight WHERE handle = ? AND verb = ?", (handle, verb)
            ).fetchone()
        return str(row["action_id"]) if row else None

    # -- audit ----------------------------------------------------------
    def insert_audit(self, row: AuditRow) -> None:
        """The only audit write. There is no update, no delete, no upsert."""
        if row.event not in EVENTS:
            raise StoreError(f"unknown audit event {row.event!r}")
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit (at, event, handle, identity, actor, actor_source, verb, reason,"
                " state, action_id, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row.at, row.event, row.handle, row.identity, row.actor, row.actor_source,
                 row.verb, row.reason, row.state, row.action_id, row.detail),
            )

    def audit(
        self, *, handle: str | None = None, event: str | None = None, limit: int = 500
    ) -> list[AuditRow]:
        sql = "SELECT * FROM audit"
        clauses: list[str] = []
        args: list[Any] = []
        if handle:
            clauses.append("handle = ?")
            args.append(handle)
        if event:
            clauses.append("event = ?")
            args.append(event)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY seq LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_audit(row) for row in rows]

    # -- credentials ----------------------------------------------------
    def insert_credential(self, record: CredentialRecord) -> CredentialRecord:
        if record.state not in CREDENTIAL_STATES:
            raise StoreError(f"unknown credential state {record.state!r}")
        with self._lock:
            try:
                self._begin()
                self._conn.execute(
                    "INSERT INTO credentials (id, instance, scope, boot_id, launch_ref, locator,"
                    " issued_at, expires_at, state, unrevocable, revoked_at, revoked_reason, note)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (record.id, record.instance, record.scope, record.boot_id, record.launch_ref,
                     record.locator, record.issued_at, record.expires_at, record.state,
                     int(record.unrevocable), record.revoked_at, record.revoked_reason, record.note),
                )
                self._commit()
            except sqlite3.IntegrityError as exc:
                self._rollback()
                raise StoreError(
                    "a live credential already holds this locator "
                    f"({record.instance}, {record.scope}); a collision proves the locator is "
                    "not deterministic per (instance, scope)"
                ) from exc
            except Exception:
                self._rollback()
                raise
        return self.get_credential(record.id)  # type: ignore[return-value]

    def get_credential(self, credential_id: str) -> CredentialRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM credentials WHERE id = ?", (credential_id,)
            ).fetchone()
        return _credential(row) if row else None

    def credentials(self, *, instance: str | None = None, state: str | None = None,
                    scope: str | None = None) -> list[CredentialRecord]:
        sql = "SELECT * FROM credentials"
        clauses: list[str] = []
        args: list[Any] = []
        for column, value in (("instance", instance), ("state", state), ("scope", scope)):
            if value is not None:
                clauses.append(f"{column} = ?")
                args.append(value)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY issued_at, rowid"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_credential(row) for row in rows]

    def promote_credential(self, credential_id: str, *, locator: str, at: str) -> CredentialRecord:
        """pending -> open, but only for the locator the adapter derived (D-4)."""
        with self._lock:
            row = self.get_credential(credential_id)
            if row is None:
                raise StoreError(f"no such credential: {credential_id}")
            if row.state != "pending":
                raise StoreError(f"credential {credential_id} is {row.state}, not pending")
            if row.locator != locator:
                raise StoreError(
                    "refusing to promote a row whose locator the adapter did not derive"
                )
            self._conn.execute(
                "UPDATE credentials SET state = 'open', issued_at = ? WHERE id = ? AND state = 'pending'",
                (at, credential_id),
            )
        return self.get_credential(credential_id)  # type: ignore[return-value]

    def fail_credential(self, credential_id: str, *, note: str) -> CredentialRecord:
        """pending -> failed. A mint that did not happen is never open."""
        with self._lock:
            row = self.get_credential(credential_id)
            if row is None:
                raise StoreError(f"no such credential: {credential_id}")
            if row.state != "pending":
                raise StoreError(f"credential {credential_id} is {row.state}, not pending")
            self._conn.execute(
                "UPDATE credentials SET state = 'failed', note = ? WHERE id = ? AND state = 'pending'",
                (note, credential_id),
            )
        return self.get_credential(credential_id)  # type: ignore[return-value]

    def revoke_credential(self, credential_id: str, *, at: str, reason: str,
                          note: str = "") -> CredentialRecord:
        """Mark a verified revocation. Allowed from `pending` too (D-4b: the sweeper
        revokes by locator before a pending row was ever promoted)."""
        with self._lock:
            row = self.get_credential(credential_id)
            if row is None:
                raise StoreError(f"no such credential: {credential_id}")
            if row.state not in ("open", "pending"):
                raise StoreError(f"credential {credential_id} is {row.state}, not revocable")
            self._conn.execute(
                "UPDATE credentials SET state = 'revoked', revoked_at = ?, revoked_reason = ?, note = ?"
                " WHERE id = ? AND state IN ('open', 'pending')",
                (at, reason, note, credential_id),
            )
        return self.get_credential(credential_id)  # type: ignore[return-value]

    def note_credential(self, credential_id: str, *, note: str) -> CredentialRecord:
        """An honest note on a row that stays open (the revoke did not confirm).

        F-4: a `pending` row takes the same note. A pending row is D-4(b)'s
        sweeper path, and losing the reason there would make an unrevoked
        credential look unattempted.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE credentials SET note = ? WHERE id = ? AND state IN ('open', 'pending')",
                (note, credential_id),
            )
        return self.get_credential(credential_id)  # type: ignore[return-value]

    def mark_unrevocable(self, credential_id: str, *, note: str) -> CredentialRecord:
        """C-02: a credential nobody can close is recorded as such, and stays open."""
        with self._lock:
            row = self.get_credential(credential_id)
            if row is None:
                raise StoreError(f"no such credential: {credential_id}")
            if row.state not in ("open", "pending"):
                raise StoreError(f"credential {credential_id} is {row.state}; unrevocable applies to live rows")
            self._conn.execute(
                "UPDATE credentials SET unrevocable = 1, note = ? WHERE id = ? AND state IN ('open', 'pending')",
                (note, credential_id),
            )
        return self.get_credential(credential_id)  # type: ignore[return-value]

    # -- launches (the core's own record; never authority, O-01) ---------
    def record_launch(self, handle: str, *, boot_id: str, identity: str, state: str,
                      ending: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO launches (handle, boot_id, identity, state, ending)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(handle) DO UPDATE SET boot_id = excluded.boot_id,"
                " identity = excluded.identity, state = excluded.state, ending = excluded.ending",
                (handle, boot_id, identity, state, ending),
            )

    def launch(self, handle: str) -> dict[str, str] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM launches WHERE handle = ?", (handle,)).fetchone()
        return dict(row) if row else None

    def launches(self) -> list[dict[str, str]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM launches ORDER BY handle").fetchall()
        return [dict(row) for row in rows]


def _action(row: sqlite3.Row) -> ActionRow:
    return ActionRow(
        id=row["id"], handle=row["handle"], verb=row["verb"], state=row["state"],
        said=row["said"], actor=row["actor"], reason=row["reason"], boot_id=row["boot_id"],
        created_at=row["created_at"], actor_source=row["actor_source"],
        started_at=row["started_at"], finished_at=row["finished_at"],
    )


def _audit(row: sqlite3.Row) -> AuditRow:
    return AuditRow(
        event=row["event"], handle=row["handle"], at=row["at"], identity=row["identity"],
        actor=row["actor"], actor_source=row["actor_source"], verb=row["verb"],
        reason=row["reason"], state=row["state"], action_id=row["action_id"],
        detail=row["detail"],
    )


def _credential(row: sqlite3.Row) -> CredentialRecord:
    return CredentialRecord(
        id=row["id"], instance=row["instance"], scope=row["scope"], boot_id=row["boot_id"],
        launch_ref=row["launch_ref"], locator=row["locator"], issued_at=row["issued_at"],
        expires_at=row["expires_at"], state=row["state"], unrevocable=bool(row["unrevocable"]),
        revoked_at=row["revoked_at"], revoked_reason=row["revoked_reason"], note=row["note"],
    )
