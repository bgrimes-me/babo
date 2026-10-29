"""Checkpoint turns: the harness asks, the resident writes, the harness files it.

When a checkpoint is due, the harness sends one extra turn into the resident's
own context (everything the model just saw, plus a short request marked as
coming from the harness). The resident answers with the checkpoint schema in
its own words. The harness validates the answer, retries once with the
validation error, and if that also fails writes a mechanical checkpoint from
the event log so the baton is never blank.

The exchange is recorded in the session archive and the event log, but it is
never added to the conversation the resident carries forward.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from loguru import logger

from babo.baton.baton_file import Checkpoint, append_checkpoint, current_entry
from babo.baton.clock import stamp
from babo.baton.config import BatonConfig
from babo.baton.events import append_event, session_slug
from babo.baton.model_client import BatonModelClient, ModelUnavailableError
from babo.baton.notes import Note, first_line, join_frontmatter, write_note
from babo.baton.registry import UNCHOSEN, Registry
from babo.baton.runtime_state import CounterStore
from babo.baton.schemas import CHECKPOINT_SCHEMA, FOLD_SCHEMA, CheckpointReply, parse_reply
from babo.baton.search import index_file
from babo.baton.town import BatonError, Town, atomic_write, now_iso, slugify, today

HARNESS_SENDER = "baton-harness"
CHECKPOINT_MARK = "[baton:checkpoint]"

_REQUEST = """{mark} This message is from the Baton harness, not from Squid. It is a checkpoint (reason: {trigger}).
Time now: {now}. You are {who}.
{fold_intro}Write your checkpoint in your own words, as JSON:
- thread: short name of what you are working on (reuse the name if it continues: "{thread}")
- doing: one sentence, what you are in the middle of
- done, learned, next, blockers: short lists (may be empty)
- confidence: 0 to 1, how sure you are of this account
- knowledge_ops: notes to keep in your knowledge/ (op create|update|supersede, title, body, path ("" for create), aliases, keywords). Add aliases and keywords so you can find it again.
- promote: notes of yours worth sharing with the town, as {{"path": "knowledge/<file>.md", "reason": "..."}}
{extra}Only the JSON. Nothing else is expected of you; after this the conversation continues where it was."""

_FOLD_EXTRA = "- revise_self: the full new text of your SELF.md if you want to revise it tonight, otherwise \"\"\n"
_FOLD_INTRO = (
    "Before you write, look back over the day: did you answer to a name, a voice or a job that isn't yours, "
    "or work in another resident's lane? If so, say so in `learned`, and set SELF.md straight with revise_self if it needs it.\n"
)


@dataclass
class CheckpointOutcome:
    checkpoint: Checkpoint
    authored: bool
    attempts: int
    error: str = ""
    notes_written: list[str] | None = None
    promoted: list[str] | None = None


class CheckpointService:
    """Runs checkpoint (and fold) turns for one resident."""

    def __init__(self, town: Town, resident_id: str, config: BatonConfig, client: BatonModelClient | None = None):
        self.town = town
        self.resident_id = resident_id
        self.config = config
        self.client = client or BatonModelClient(config.model, resident=resident_id)
        self.counters = CounterStore(town, resident_id)

    @property
    def home(self) -> Path:
        return self.town.resident_dir(self.resident_id)

    # -- the turn --------------------------------------------------------
    def request_text(self, trigger: str, *, fold: bool = False) -> str:
        last = current_entry(self.home)
        resident = Registry(self.town).get(self.resident_id)
        who = f"the resident `{self.resident_id}`"
        if resident is not None:
            who += f", office: {resident.office}" if resident.name == UNCHOSEN else f", named {resident.name}, office: {resident.office}"
        return _REQUEST.format(
            mark=CHECKPOINT_MARK, trigger=trigger, thread=last.thread if last else "general",
            now=stamp(datetime.now().astimezone()), who=who,
            fold_intro=_FOLD_INTRO if fold else "", extra=_FOLD_EXTRA if fold else "",
        )

    async def ask(self, messages: list[dict[str, Any]], trigger: str, *, fold: bool = False, priority: str = "interactive") -> tuple[CheckpointReply | None, int, str, list[dict[str, Any]]]:
        """Ask for a checkpoint. Returns (reply or None, attempts, last error, transcript)."""
        request = {"role": "user", "content": self.request_text(trigger, fold=fold), "_sender": HARNESS_SENDER}
        convo = [*messages, request]
        schema = FOLD_SCHEMA if fold else CHECKPOINT_SCHEMA
        exchange: list[dict[str, Any]] = [request]
        error = ""
        flatten = False
        for attempt in (1, 2):
            try:
                result = await self.client.chat_json(convo, schema, name="fold" if fold else "checkpoint", priority=priority, flatten=flatten)
            except ModelUnavailableError as exc:
                return None, attempt, f"model unavailable: {exc}", exchange
            exchange.append({"role": "assistant", "content": result.raw, "_sender": self.resident_id})
            if result.data is None:
                error = result.error or "no JSON in the reply"
                # A server error on tool-call history: try again with plain-text history.
                flatten = flatten or result.raw == ""
            else:
                reply, error = parse_reply(result.data)
                if reply is not None:
                    return reply, attempt, "", exchange
            retry = {
                "role": "user",
                "content": f"{CHECKPOINT_MARK} That checkpoint could not be filed ({error}). Please send it again as JSON matching the fields above.",
                "_sender": HARNESS_SENDER,
            }
            convo = [*convo, {"role": "assistant", "content": result.raw or "(no reply)"}, retry]
            exchange.append(retry)
        return None, 2, error, exchange

    # -- filing ----------------------------------------------------------
    def _own_knowledge_path(self, ref: str) -> Path | None:
        ref = ref.strip().strip("/")
        if not ref:
            return None
        if ref.startswith(f"residents/{self.resident_id}/"):
            ref = ref.split("/", 2)[2]
        path = (self.home / ref).resolve()
        knowledge = (self.home / "knowledge").resolve()
        if path.suffix != ".md" or not path.is_relative_to(knowledge):
            return None
        return path

    def apply_knowledge(self, reply: CheckpointReply) -> list[str]:
        written: list[str] = []
        knowledge = self.home / "knowledge"
        for op in reply.knowledge_ops:
            try:
                target = self._own_knowledge_path(op.path) if op.path else None
                if op.op == "supersede" and target is not None and target.exists():
                    old = Note.load(target)
                    new = write_note(knowledge, title=op.title, body=op.body, author=self.resident_id,
                                     aliases=op.aliases, keywords=op.keywords,
                                     supersedes=str(target.relative_to(self.home)))
                    if new.path.resolve() != old.path.resolve():
                        old.status = "superseded"
                        old.extra["superseded_by"] = str(new.path.relative_to(self.home))
                        old.updated = now_iso()
                        old.save()
                        index_file(self.town, self.config.index_file, old.path)
                    note = new
                elif op.op == "update" and target is not None and target.exists():
                    note = write_note(knowledge, title=op.title, body=op.body, author=self.resident_id,
                                      aliases=op.aliases, keywords=op.keywords, path=target)
                else:
                    note = write_note(knowledge, title=op.title, body=op.body, author=self.resident_id,
                                      aliases=op.aliases, keywords=op.keywords)
                index_file(self.town, self.config.index_file, note.path)
                written.append(str(note.path.relative_to(self.home)))
            except (OSError, ValueError, BatonError) as exc:
                logger.warning("Baton: knowledge op for {} skipped: {}", self.resident_id, exc)
        return written

    def apply_promotions(self, reply: CheckpointReply) -> list[str]:
        promoted: list[str] = []
        for item in reply.promote:
            path = self._own_knowledge_path(item.path)
            if path is None or not path.exists():
                continue
            note = Note.load(path)
            rel = str(path.relative_to(self.town.root))
            meta = {
                "from": self.resident_id,
                "note": rel,
                "title": note.title,
                "reason": item.reason.strip(),
                "created": now_iso(),
                "status": "pending",
                "aliases": note.aliases,
                "keywords": note.keywords,
            }
            target = self.town.promotions / f"{today()}-{self.resident_id}-{slugify(note.title)}.md"
            # The Archivist can't read residents' directories, so the nomination carries the note.
            atomic_write(target, join_frontmatter(meta, note.body))
            promoted.append(str(target.relative_to(self.town.root)))
        return promoted

    def file(self, reply: CheckpointReply, *, trigger: str, session_key: str | None) -> CheckpointOutcome:
        counters = self.counters.load()
        cp = Checkpoint(
            thread=reply.thread, doing=reply.doing, done=reply.done, learned=reply.learned,
            next=reply.next, blockers=reply.blockers, confidence=reply.confidence,
            source="authored", trigger=trigger, resident=self.resident_id,
            session=session_key or "", tool_calls=counters.calls_since_checkpoint,
        )
        written = self.apply_knowledge(reply)
        promoted = self.apply_promotions(reply)
        append_checkpoint(self.home, cp)
        self.counters.update(calls_since_checkpoint=0, work_since_checkpoint=False, last_checkpoint_at=cp.at)
        return CheckpointOutcome(cp, True, 1, notes_written=written, promoted=promoted)

    def mechanical(self, events: list[dict[str, Any]], *, trigger: str, session_key: str | None, error: str) -> CheckpointOutcome:
        last = current_entry(self.home)
        tools = [e for e in events if e.get("kind") == "tool"]
        done: list[str] = []
        blockers: list[str] = []
        for e in tools[-25:]:
            args: object = e.get("args")
            command = cast(dict[str, Any], args).get("command") if isinstance(args, dict) else None
            label = f"ran `{first_line(str(command), 80)}`" if command else f"{e.get('tool')} {', '.join(e.get('files') or [])}".strip()
            if e.get("exit_code") is not None:
                label += f" (exit {e.get('exit_code')})"
            if e.get("test"):
                label += f" [tests {e.get('test')}]"
            done.append(label)
            if e.get("error"):
                blockers.append(f"{e.get('tool')} failed: {first_line(str(e.get('preview', '')), 100)}")
        counters = self.counters.load()
        cp = Checkpoint(
            thread=last.thread if last else "general",
            doing=f"Mechanical record: {len(tools)} tool call(s) since the last checkpoint. The checkpoint turn failed ({error}).",
            done=done, blockers=blockers[-10:], confidence=0.0, source="mechanical", trigger=trigger,
            resident=self.resident_id, session=session_key or "", tool_calls=counters.calls_since_checkpoint,
        )
        append_checkpoint(self.home, cp)
        self.counters.update(calls_since_checkpoint=0, work_since_checkpoint=False, last_checkpoint_at=cp.at)
        return CheckpointOutcome(cp, False, 2, error=error)

    def archive_exchange(self, session_key: str | None, exchange: list[dict[str, Any]], outcome: CheckpointOutcome) -> None:
        """The checkpoint turn is visible in the session archive (not in the carried context)."""
        day_dir = self.home / "sessions" / today()
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"{session_slug(session_key)}.checkpoints.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": now_iso(), "trigger": outcome.checkpoint.trigger, "authored": outcome.authored,
                                     "exchange": exchange}, ensure_ascii=False, default=str) + "\n")

    # -- the whole thing ---------------------------------------------------
    async def run(
        self,
        messages: list[dict[str, Any]],
        *,
        trigger: str,
        session_key: str | None,
        events: list[dict[str, Any]] | None = None,
        fold: bool = False,
        priority: str = "interactive",
    ) -> CheckpointOutcome:
        reply, attempts, error, exchange = await self.ask(messages, trigger, fold=fold, priority=priority)
        if reply is not None:
            outcome = self.file(reply, trigger=trigger, session_key=session_key)
            outcome.attempts = attempts
            if fold and reply.revise_self.strip():
                from babo.baton.tools.core import Toolbox

                Toolbox(self.town, self.resident_id, self.config.index_file).revise_self(reply.revise_self, reason="nightly fold")
        else:
            outcome = self.mechanical(events or [], trigger=trigger, session_key=session_key, error=error)
        self.archive_exchange(session_key, exchange, outcome)
        append_event(
            self.town, self.resident_id, session_key, "checkpoint",
            trigger=trigger, authored=outcome.authored, attempts=outcome.attempts, error=outcome.error,
            thread=outcome.checkpoint.thread, notes=outcome.notes_written or [], promoted=outcome.promoted or [],
        )
        logger.info("Baton: {} checkpoint for {} ({}, {})", "authored" if outcome.authored else "mechanical",
                    self.resident_id, trigger, outcome.checkpoint.thread)
        return outcome
