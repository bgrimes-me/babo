"""Restore-or-nothing compaction for residents.

Before anything is compacted:

1. the resident writes a checkpoint (the "right before compaction" trigger);
2. the session is backed up to ``sessions/<date>/<session>.pre-compact-<time>.jsonl``
   with a SHA-256 sidecar, and the backup is read back and verified.

Then upstream's summary is requested. Upstream would fall back to a truncated
raw dump when the model fails; Baton refuses that. A compressed context is
accepted only if it is sound: the model's summary, or, if that fails, the
checkpoint the resident has just authored. Otherwise nothing happens: the
session is left exactly as it was (upstream commits summaries in one SQLite
transaction and never deletes messages), and the failure is logged and put in
the event log. The latest baton entry is always carried into the compacted
context. The journal on disk is never truncated.

``restore_session_backup`` puts a backup back into the session store when the
gateway is stopped (``nanobot baton restore-session``).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from loguru import logger

from nanobot.agent.memory import Consolidator, MemoryArchiver
from nanobot.baton.baton_file import Checkpoint, current_entry
from nanobot.baton.events import append_event, session_slug
from nanobot.baton.town import BatonError, Town, find_town_for_workspace, today
from nanobot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from nanobot.baton.hooks import MemoryFormation
    from nanobot.utils.llm_runtime import LLMRuntime

STAND_IN = (
    "The summary of this stretch could not be produced; my checkpoint, written "
    "just before compaction, stands in for it."
)


class RawFallbackRejectedError(RuntimeError):
    """The archiver tried to substitute a truncated raw dump for a summary."""


class BatonArchiver(MemoryArchiver):
    def _raw_checkpoint(self, messages: list[dict[str, Any]], **_: Any) -> str:  # pyright: ignore[reportIncompatibleMethodOverride]
        raise RawFallbackRejectedError("no sound summary came back from the model")


def baton_block(cp: Checkpoint | None) -> str:
    if cp is None:
        return ""
    return "[Latest baton entry]\n" + cp.render()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_backup(home: Path, session_key: str, messages: list[dict[str, Any]], *, extra: dict[str, Any] | None = None) -> Path:
    """Write and verify a session backup. Raises if it can't be verified."""
    day = home / "sessions" / today()
    day.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S-%f")
    path = day / f"{session_slug(session_key)}.pre-compact-{stamp}.jsonl"
    header: dict[str, Any] = {
        "session_key": session_key,
        "message_count": len(messages),
        "created": datetime.now().astimezone().isoformat(),
        **(extra or {}),
    }
    lines = [json.dumps({"header": header}, ensure_ascii=False, default=str)]
    lines += [json.dumps(m, ensure_ascii=False, default=str) for m in messages]
    data = ("\n".join(lines) + "\n").encode("utf-8")
    path.write_bytes(data)
    digest = _digest(data)
    path.with_name(path.name + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    if _digest(path.read_bytes()) != digest:
        raise BatonError(f"backup {path.name} did not verify")
    return path


def read_backup(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = path.read_bytes()
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.exists() or sidecar.read_text(encoding="utf-8").split()[0] != _digest(data):
        raise BatonError(f"backup {path.name} failed its hash check; refusing to use it")
    rows = [cast(dict[str, Any], json.loads(line)) for line in data.decode("utf-8").splitlines() if line.strip()]
    header = cast(dict[str, Any], rows[0].get("header", {}))
    return header, rows[1:]


class BatonConsolidator(Consolidator):
    """Consolidator for resident sessions (chosen by ``loop_classes``)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        upstream = self.archiver
        self.archiver = BatonArchiver(
            store=upstream.store,
            build_messages=self._build_messages,
            get_tool_definitions=self._get_tool_definitions,
            resolve_prompt_context=upstream._resolve_prompt_context,  # pyright: ignore[reportPrivateUsage]
        )
        self.formation: MemoryFormation | None = None
        self._last_authored: dict[str, Checkpoint] = {}

    # -- shared steps --------------------------------------------------------
    @property
    def _town_resident(self) -> tuple[Town, str]:
        found = find_town_for_workspace(self.store.workspace)
        if found is None:
            raise BatonError("BatonConsolidator used outside a resident workspace")
        return found

    async def _prepare(
        self, session_key: str, messages: list[dict[str, Any]], stored: list[dict[str, Any]], *, watermark: int = 0,
    ) -> Checkpoint | None:
        """Pre-compaction checkpoint, then a verified backup. Returns the authored
        checkpoint (or None); raises if the backup could not be verified."""
        town, resident_id = self._town_resident
        authored: Checkpoint | None = None
        if self.formation is not None:
            try:
                outcome = await self.formation.checkpoint_now(session_key, messages, "compaction")
                if outcome.authored:
                    authored = outcome.checkpoint
            except Exception:
                logger.exception("Baton: pre-compaction checkpoint failed for {}", resident_id)
        backup = write_backup(town.resident_dir(resident_id), session_key, stored, extra={"reason": "pre-compaction", "last_consolidated": watermark})
        append_event(town, resident_id, session_key, "compaction", phase="backup", backup=backup.name)
        return authored

    def _with_baton(self, summary: str) -> str:
        town, resident_id = self._town_resident
        block = baton_block(current_entry(town.resident_dir(resident_id)))
        return f"{summary.rstrip()}\n\n{block}" if block else summary

    def _refused(self, session_key: str, why: str) -> None:
        town, resident_id = self._town_resident
        logger.error("Baton: compaction refused for {} ({}); nothing was changed", session_key, why)
        append_event(town, resident_id, session_key, "compaction", phase="refused", error=why)

    async def _stored(self, session_key: str) -> tuple[list[dict[str, Any]], int, Session | None]:
        session = await self.sessions.state.read(session_key)
        if session is None:
            return [], 0, None
        return list(session.messages), session.last_consolidated, session

    # -- in-turn compaction (context pressure) ------------------------------
    async def summarize_transcript(
        self,
        accepted_messages: list[dict[str, Any]],
        previous_summary: str | None,
        *,
        runtime: LLMRuntime,
        session_key: str,
        tools: list[dict[str, Any]],
        provider_state: Any = None,
        persist: bool = True,
    ) -> str | None:
        if not persist:
            return await super().summarize_transcript(
                accepted_messages, previous_summary, runtime=runtime, session_key=session_key,
                tools=tools, provider_state=provider_state, persist=persist,
            )
        try:
            stored, watermark, _ = await self._stored(session_key)
            authored = await self._prepare(session_key, accepted_messages, stored or accepted_messages, watermark=watermark)
        except Exception as exc:
            self._refused(session_key, f"backup failed: {exc}")
            return None
        try:
            summary = await super().summarize_transcript(
                accepted_messages, previous_summary, runtime=runtime, session_key=session_key,
                tools=tools, provider_state=provider_state, persist=persist,
            )
        except RawFallbackRejectedError:
            summary = None
        if not summary:
            if authored is None:
                self._refused(session_key, "no sound summary and no authored checkpoint")
                return None
            summary = STAND_IN
        return self._with_baton(summary)

    # -- idle / manual compaction ----------------------------------------------
    async def archive_session(self, session: Session, *, archive_end: int, runtime: LLMRuntime) -> str | None:
        try:
            summary = await super().archive_session(session, archive_end=archive_end, runtime=runtime)
        except RawFallbackRejectedError:
            if session.key not in self._last_authored:
                raise
            summary = STAND_IN
        return self._with_baton(summary) if summary else summary

    async def compact_idle_session(self, session_key: str, **kwargs: Any) -> str | None:
        stored, watermark, session = await self._stored(session_key)
        if not stored or session is None:
            return await super().compact_idle_session(session_key, **kwargs)
        try:
            authored = await self._prepare(session_key, session.get_history(), stored, watermark=watermark)
        except Exception as exc:
            self._refused(session_key, f"backup failed: {exc}")
            return None
        if authored is not None:
            self._last_authored[session_key] = authored
        try:
            return await super().compact_idle_session(session_key, **kwargs)
        except RawFallbackRejectedError:
            self._refused(session_key, "no sound summary and no authored checkpoint")
            return None
        finally:
            self._last_authored.pop(session_key, None)


def restore_session_backup(sessions: SessionManager, backup: Path) -> int:
    """Offline restore (gateway stopped): put the backed-up messages back as
    the session's whole history. Returns the number of messages restored."""
    header, messages = read_backup(backup)
    key = str(header.get("session_key") or "")
    if not key:
        raise BatonError("backup has no session key")
    current = sessions.get_or_create(key)
    current.messages = messages
    current.last_consolidated = int(header.get("last_consolidated") or 0)
    sessions.save(current)
    return len(messages)
