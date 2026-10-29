"""Memory formation as fixed infrastructure: the per-turn Baton hook.

Records every tool call to the event log, watches for checkpoint triggers, and
runs the checkpoint turn at the next safe point (right after a tool round, with
the exact messages the model just worked from). There is no switch that turns
this off; only its timing is configurable.

Triggers: a git commit, tests flipping between pass and fail, every N tool
calls, right before compaction (from ``BatonConsolidator``), the session going
idle or ending after work, and the resident's own ``checkpoint()``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from loguru import logger

from nanobot.agent.hook import (
    AgentHook,
    AgentHookContext,
    AgentRunHookContext,
    AgentTurnHookContext,
)
from nanobot.baton.checkpoint import CheckpointOutcome, CheckpointService
from nanobot.baton.events import append_event, classify, events_path, read_events, tool_record
from nanobot.baton.registry import Registry
from nanobot.baton.runtime_state import CounterStore
from nanobot.baton.town import Town, now_iso

if TYPE_CHECKING:
    from nanobot.baton.config import BatonConfig
    from nanobot.providers.base import ToolCallRequest


@dataclass
class MemoryFormation:
    """Per-resident state shared by every turn hook of one AgentLoop."""

    town: Town
    resident_id: str
    config: BatonConfig
    service: CheckpointService
    due: dict[str, str] = field(default_factory=dict)  # session -> trigger
    last_messages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    idle_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Test seam: replaces asyncio.sleep for idle timing.
    sleep: Callable[[float], Any] = asyncio.sleep

    @property
    def counters(self) -> CounterStore:
        return self.service.counters

    def mark_due(self, session_key: str | None, trigger: str) -> None:
        key = session_key or "direct"
        # The first reason wins; later ones in the same round don't need another checkpoint.
        self.due.setdefault(key, trigger)

    def events_since_checkpoint(self, session_key: str | None) -> list[dict[str, Any]]:
        since = self.counters.load().last_checkpoint_at
        records = read_events(events_path(self.town, self.resident_id, session_key))
        return [r for r in records if str(r.get("at", "")) > since]

    async def checkpoint_now(self, session_key: str | None, messages: list[dict[str, Any]], trigger: str) -> CheckpointOutcome:
        async with self._lock:
            self.due.pop(session_key or "direct", None)
            return await self.service.run(
                [dict(m) for m in messages], trigger=trigger, session_key=session_key,
                events=self.events_since_checkpoint(session_key),
            )

    # -- idle / session end ------------------------------------------------
    def cancel_idle(self, session_key: str | None) -> None:
        task = self.idle_tasks.pop(session_key or "direct", None)
        if task is not None and not task.done():
            task.cancel()

    def schedule_idle(self, session_key: str | None, messages: list[dict[str, Any]]) -> None:
        key = session_key or "direct"
        self.cancel_idle(key)
        self.last_messages[key] = [dict(m) for m in messages]

        async def wait_then_checkpoint() -> None:
            try:
                await self.sleep(self.config.memory.idle_minutes * 60)
                if self.counters.load().work_since_checkpoint:
                    await self.checkpoint_now(session_key, self.last_messages.get(key, []), "idle")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Baton: idle checkpoint failed for {}", self.resident_id)
            finally:
                self.idle_tasks.pop(key, None)

        try:
            self.idle_tasks[key] = asyncio.get_running_loop().create_task(wait_then_checkpoint())
        except RuntimeError:
            pass

    async def flush(self) -> None:
        """Session end: checkpoint any session that did work since its last checkpoint."""
        for key, task in list(self.idle_tasks.items()):
            task.cancel()
            self.idle_tasks.pop(key, None)
        if not self.counters.load().work_since_checkpoint:
            return
        for key, messages in list(self.last_messages.items()):
            if self.counters.load().work_since_checkpoint:
                try:
                    await self.checkpoint_now(key, messages, "session_end")
                except Exception:
                    logger.exception("Baton: session-end checkpoint failed for {}", self.resident_id)

    # -- hooks ---------------------------------------------------------------
    def hook_factory(self, turn: AgentTurnHookContext) -> AgentHook | None:
        if turn.ephemeral:
            return None
        return BatonTurnHook(self, turn.session_key)


class BatonTurnHook(AgentHook):
    def __init__(self, formation: MemoryFormation, session_key: str | None):
        super().__init__()
        self.f = formation
        self.session_key = session_key
        self._tool_calls = 0

    async def before_run(self, context: AgentRunHookContext) -> None:
        self.f.cancel_idle(self.session_key)
        append_event(self.f.town, self.f.resident_id, self.session_key, "turn_start")

    def _record(self, tool_call: ToolCallRequest, result: Any, *, error: bool) -> None:
        raw: object = tool_call.arguments
        args: dict[str, Any] = cast(dict[str, Any], raw) if isinstance(raw, dict) else {"raw": str(raw)}
        event = classify(tool_call.name, args, result, error=error)
        append_event(self.f.town, self.f.resident_id, self.session_key, "tool", **tool_record(event))
        self._tool_calls += 1
        counters = self.f.counters.bump()
        if event.commit:
            self.f.mark_due(self.session_key, "commit")
        if event.test is not None:
            if counters.last_test is not None and counters.last_test != event.test:
                self.f.mark_due(self.session_key, "tests_flipped")
            self.f.counters.update(last_test=event.test)
        if counters.calls_since_checkpoint >= self.f.config.memory.checkpoint_every_tool_calls:
            self.f.mark_due(self.session_key, "interval")

    async def after_execute_tool(self, context: AgentHookContext, tool_call: ToolCallRequest, tool: Any, params: Any, result: Any) -> None:
        self._record(tool_call, result, error=False)

    async def on_execute_tool_error(self, context: AgentHookContext, tool_call: ToolCallRequest, tool: Any, params: Any, error: Any) -> None:
        self._record(tool_call, error, error=True)

    async def after_iteration(self, context: AgentHookContext) -> None:
        key = self.session_key or "direct"
        trigger = self.f.due.get(key)
        if trigger is None or not context.tool_calls:
            return
        await self.f.checkpoint_now(self.session_key, context.messages, trigger)

    async def after_run(self, context: AgentRunHookContext) -> None:
        append_event(self.f.town, self.f.resident_id, self.session_key, "turn_end", tool_calls=self._tool_calls)
        try:
            Registry(self.f.town).update(self.f.resident_id, last_active=now_iso())
        except Exception:
            logger.exception("Baton: could not update last_active for {}", self.f.resident_id)
        key = self.session_key or "direct"
        if key in self.f.due:
            # Due but no tool round followed (e.g. checkpoint() as the last call).
            await self.f.checkpoint_now(self.session_key, context.messages, self.f.due[key])
        if self.f.counters.load().work_since_checkpoint:
            self.f.schedule_idle(self.session_key, context.messages)
