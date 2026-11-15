"""The agent's admission gate: one refusal shape, five paths, and no bypass.

`quiesce` means *stop admitting new work*. These rows are about the word being
true, which is why they are written against every path that can start a turn and
not just the HTTP one:

- the default is open, and nothing changes when nobody quiesces — a gate that is
  closed by accident is worse than no gate;
- while quiesced, every path refuses with a **sentence**, including idle
  compaction and the SDK, which reach the lock factory from outside the loop;
- `resume` reopens all of them. A gate you cannot lift is a wedge with better
  manners;
- `_get_session_lock` has exactly one caller, inside `_admit`. That is the row
  that turns "the gate is above creation" from remembered into structural, and it
  is the one that will still be doing its job in six months.
"""

from __future__ import annotations

import ast
import asyncio
import weakref
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from babo.agent.autocompact import AutoCompact
from babo.agent.loop import AgentLoop, Quiesced, TurnRecord
from babo.sdk.clients import RuntimeClient

LOOP_SOURCE = Path(__file__).resolve().parents[2] / "babo" / "agent" / "loop.py"


def _bare_loop() -> AgentLoop:
    """An AgentLoop with only the admission state, built the way the suite does."""
    loop = AgentLoop.__new__(AgentLoop)
    loop._session_locks = weakref.WeakValueDictionary()
    loop._active_turns = {}
    loop._active_tasks = {}
    loop._pending_queues = {}
    loop._quiesce = None
    loop._concurrency_gate = None
    return loop


def _auto_compact(loop: AgentLoop) -> tuple[AutoCompact, MagicMock]:
    consolidator = MagicMock()
    consolidator.compact_idle_session = AsyncMock(return_value=None)
    compactor = AutoCompact(
        sessions=MagicMock(),
        consolidator=consolidator,
        session_ttl_minutes=1,
        session_lock=loop.admit,
    )
    return compactor, consolidator


# -- the default is open ------------------------------------------------------
def test_the_default_admits_everything_so_nothing_changes_when_nobody_quiesces() -> None:
    loop = _bare_loop()
    admitted = [loop.admit(f"cli:{n}") for n in range(4)]
    assert all(isinstance(item, asyncio.Lock) for item in admitted)
    assert loop.admit("cli:0") is admitted[0], "the same session must get the same lock"
    assert loop.quiesced is None


def test_a_turn_registry_starts_empty_and_reports_nothing() -> None:
    loop = _bare_loop()
    assert loop.active_turns() == []


# -- quiesce refuses every path, with a sentence ------------------------------
def test_quiesce_refuses_admission_with_a_sentence() -> None:
    loop = _bare_loop()
    loop.quiesce("sessions leaked")
    refused = loop.admit("cli:run")
    assert isinstance(refused, Quiesced)
    assert "sessions leaked" in refused.sentence
    assert "not admitting new work" in refused.sentence


def test_quiesce_creates_no_lock_for_the_work_it_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate is above creation: a refusal must not even ask for a lock.

    Membership cannot prove this. ``_session_locks`` is a WeakValueDictionary, so a
    lock created and then dropped is collected before anybody can look for it — the
    first version of this row passed against a gate that created the lock first.
    The row spies on the factory instead, which is the only thing that can tell the
    difference between "not created" and "created and forgotten".
    """
    loop = _bare_loop()
    created: list[str] = []
    original = loop._get_session_lock

    def recording(key: str) -> asyncio.Lock:
        created.append(key)
        return original(key)

    monkeypatch.setattr(loop, "_get_session_lock", recording)
    loop.quiesce("stand down")
    loop.admit("cli:never")
    assert created == []


def test_quiesce_stops_process_direct_before_it_takes_a_turn() -> None:
    loop = _bare_loop()
    loop.quiesce("stand down")
    result = asyncio.run(loop.process_direct("hello", session_key="cli:direct"))
    assert result is None


def test_quiesce_stops_the_queue_worker_and_leaves_the_message_in_its_inbox() -> None:
    """Admission stops; already-admitted work is not discarded."""
    loop = _bare_loop()
    pending: asyncio.Queue[Any] = asyncio.Queue()
    pending.put_nowait("a queued message")
    loop._pending_queues["cli:queued"] = pending
    loop.quiesce("stand down")
    asyncio.run(loop._run_session_queue("cli:queued", pending))
    assert pending.qsize() == 1, "the admitted message must not be discarded"
    assert loop._pending_queues["cli:queued"] is pending, "resume needs a queue to restart"


def test_quiesce_stops_idle_compaction() -> None:
    loop = _bare_loop()
    compactor, consolidator = _auto_compact(loop)
    loop.quiesce("stand down")
    asyncio.run(compactor._archive("cli:idle", runtime=MagicMock()))
    consolidator.compact_idle_session.assert_not_awaited()
    assert "cli:idle" not in compactor._archiving


def test_quiesce_reaches_the_sdk_and_is_not_swallowed_by_its_none() -> None:
    """`None` means "nothing to compact"; a refusal must not read as that."""
    loop = _bare_loop()
    loop.sessions = MagicMock()
    client = RuntimeClient(loop)
    loop.quiesce("stand down")
    refused = asyncio.run(client.compact_idle_session("cli:history"))
    assert isinstance(refused, Quiesced)
    assert refused is not None


def test_quiesce_reaches_the_sdk_compaction_and_it_keeps_its_own_shape() -> None:
    loop = _bare_loop()
    loop.sessions = MagicMock()
    client = RuntimeClient(loop)
    loop.quiesce("stand down")
    refused = asyncio.run(client.compact_session("cli:history"))
    assert isinstance(refused, Quiesced)


def test_a_second_quiesce_keeps_the_first_reason() -> None:
    loop = _bare_loop()
    loop.quiesce("sessions leaked")
    loop.quiesce("something else")
    refused = loop.admit("cli:run")
    assert isinstance(refused, Quiesced) and "sessions leaked" in refused.sentence


# -- resume reopens it --------------------------------------------------------
def test_resume_admits_again() -> None:
    loop = _bare_loop()
    loop.quiesce("stand down")
    loop.resume()
    assert isinstance(loop.admit("cli:run"), asyncio.Lock)
    assert loop.quiesced is None


# -- the structural row -------------------------------------------------------
def _callers_of(name: str) -> list[tuple[str, int]]:
    tree = ast.parse(LOOP_SOURCE.read_text())
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr != name:
            continue
        owner = node
        while owner in parents and not isinstance(owner, ast.FunctionDef):
            owner = parents[owner]
        found.append((owner.name if isinstance(owner, ast.FunctionDef) else "<module>", node.lineno))
    return found


def test_get_session_lock_has_exactly_one_caller_and_it_is_admit() -> None:
    """The gate is structural, not remembered.

    A row that only checks behaviour would pass forever while somebody added a
    sixth call site beside the gate — which is exactly how the second admission
    path appeared the first time.
    """
    callers = [name for name, _ in _callers_of("_get_session_lock")]
    assert callers == ["_admit"], callers


def test_the_office_routes_are_named_once_and_imported_not_retyped() -> None:
    """A path typed twice is a path that drifts in one of the two places.

    The route table must register a *name* from ``babo.office.relay``, not a
    literal: a literal that drifts by one character leaves the relay reporting
    "the door has no /v1/office/status" forever while every row stays green.
    """
    server_path = LOOP_SOURCE.parents[1] / "api" / "server.py"
    tree = ast.parse(server_path.read_text())
    registered: dict[str, ast.expr] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"add_get", "add_post"} or not node.args:
            continue
        target = node.args[0]
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            continue
        assert isinstance(target, ast.Name), ast.dump(target)
        registered[target.id] = target
    assert {"AGENT_STATUS", "AGENT_QUIESCE", "AGENT_FLUSH", "AGENT_MESSAGE"} <= set(registered)


def test_a_turn_registry_entry_becomes_the_wire_shape() -> None:
    record = TurnRecord(session_key="api:matrix:@evans", turn_id="t-9f21",
                        wall_started_at=1763201400.0)
    assert record.as_wire() == {"session": "api:matrix:@evans", "turn_id": "t-9f21",
                                "wall_started_at": record.as_wire()["wall_started_at"]}
    assert record.as_wire()["wall_started_at"].endswith("Z")
