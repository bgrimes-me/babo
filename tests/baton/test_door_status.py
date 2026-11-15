"""The door's status file: whether it is alive, how old its word is, and when it lies.

The door dies truthfully when the process it watches exits — but it can also be
killed, and a killed door writes nothing at all. These rows are about the reader
refusing to turn either of those into a confident answer:

- a clean death is written down, with the reason;
- a **killed** door leaves its last word saying ``alive: true``, so the reader ages
  it out and answers *unknown*, never alive;
- a status with no readable age is unknown for the same reason.

A stale positive is the one direction that costs somebody work: a caller told
"someone is in there" leaves a session alone that nobody is left to be in.
"""

from __future__ import annotations

import json
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from babo.baton import opencode


def _python() -> str:
    """This host can report an empty ``sys.executable``; CI never does."""
    return sys.executable or "python3"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for(check, seconds: float = 10.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if check():
            return True
        time.sleep(0.1)
    return False


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def watched() -> subprocess.Popen:
    """A process for the door to watch, and a way to end it cleanly."""
    proc = subprocess.Popen([_python(), "-c", "import time; time.sleep(120)"])
    yield proc
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture
def door(tmp_path: Path, watched: subprocess.Popen):
    """A real door process, on a real socket, watching ``watched``."""
    status = tmp_path / "session.door.json"
    proc = subprocess.Popen(
        [_python(), "-m", "babo.baton.door", str(_free_port()), "1", str(status), str(watched.pid)],
    )
    assert _wait_for(lambda: status.exists()), "the door never wrote its status"
    yield proc, status
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)


def _presence(monkeypatch: pytest.MonkeyPatch, status: Path) -> dict:
    monkeypatch.setattr(opencode, "door_status_path", lambda town, session: status)
    return opencode.presence(None, None)  # type: ignore[arg-type]


# -- what the door says -------------------------------------------------------
def test_the_door_says_alive_the_pid_it_watches_and_when_it_wrote(door, watched, monkeypatch) -> None:
    _, status = door
    written = _read(status)
    assert written["alive"] is True and written["pid"] == watched.pid
    assert written["written_at"].endswith(("Z", "+00:00", "-05:00", "-04:00"))

    state = _presence(monkeypatch, status)
    assert state["alive"] is True and state["pid"] == watched.pid
    assert isinstance(state["age_s"], float) and state["age_s"] < 10


def test_the_door_keeps_writing_while_it_is_idle(door) -> None:
    """A heartbeat, not a one-shot: an idle door must not age out of its own file."""
    _, status = door
    first = _read(status)["written_at"]
    assert _wait_for(lambda: _read(status)["written_at"] != first, 8)


def test_the_door_listens_on_the_address_it_is_given(tmp_path: Path, watched) -> None:
    """The address is the caller's: a session's door stays inward, a pod's looks out."""
    status = tmp_path / "session.door.json"
    port = _free_port()
    proc = subprocess.Popen(
        [_python(), "-m", "babo.baton.door", str(port), "1", str(status), str(watched.pid), "0.0.0.0"],
    )
    try:
        assert _wait_for(lambda: status.exists() and _read(status)["alive"] is True, 10)
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass  # it is reachable on an interface it was told to listen on
    finally:
        proc.kill()
        proc.wait(timeout=5)


# -- a clean death is written down -------------------------------------------
def test_a_clean_death_is_written_down_with_its_reason(door, watched, monkeypatch) -> None:
    door_proc, status = door
    watched.send_signal(signal.SIGTERM)
    watched.wait(timeout=10)
    assert _wait_for(lambda: _read(status)["alive"] is False, 10)
    state = _presence(monkeypatch, status)
    assert state["alive"] is False
    assert "exited" in state["reason"]
    assert _wait_for(lambda: door_proc.poll() is not None, 10), "the door must expire with it"


# -- a killed door reads unknown ---------------------------------------------
def test_a_killed_door_reads_unknown_not_alive(door, monkeypatch) -> None:
    """SIGKILL runs no finally: the file still says alive, and the reader must not."""
    door_proc, status = door
    assert _read(status)["alive"] is True
    door_proc.kill()  # SIGKILL, deliberately not terminate
    door_proc.wait(timeout=5)
    assert _read(status)["alive"] is True, "the killed door cannot have said otherwise"

    assert _wait_for(lambda: _presence(monkeypatch, status)["alive"] is None, 12)
    state = _presence(monkeypatch, status)
    assert state["alive"] is None
    assert "aged out" in state["reason"]
    # And it is not a quiet "nobody is here": connections are not a reading either.
    assert state["connections"] == 0


# -- the reader's own guards -------------------------------------------------
def test_a_status_with_no_readable_age_is_never_alive(tmp_path, monkeypatch) -> None:
    status = tmp_path / "session.door.json"
    status.write_text(json.dumps({
        "connections": 1, "last_seen": "", "alive": True, "pid": 1234,
        "reason": "", "written_at": "not a time",
    }), encoding="utf-8")
    state = _presence(monkeypatch, status)
    assert state["alive"] is None and state["age_s"] is None


def test_a_status_older_than_the_heartbeat_is_never_alive(tmp_path, monkeypatch) -> None:
    status = tmp_path / "session.door.json"
    status.write_text(json.dumps({
        "connections": 1, "last_seen": "", "alive": True, "pid": 1234, "reason": "",
        "written_at": "2020-01-01T00:00:00-05:00",
    }), encoding="utf-8")
    assert _presence(monkeypatch, status)["alive"] is None


def test_no_status_at_all_is_unknown_not_alive(tmp_path, monkeypatch) -> None:
    state = _presence(monkeypatch, tmp_path / "missing.json")
    assert state["alive"] is None and state["connections"] == 0
    assert "no status" in state["reason"]


def test_a_clean_refusal_is_not_treated_as_a_stale_one(tmp_path, monkeypatch) -> None:
    """`alive: false` stays false however old it is: a negative is not a rumour."""
    status = tmp_path / "session.door.json"
    status.write_text(json.dumps({
        "connections": 0, "last_seen": "", "alive": False, "pid": 1234,
        "reason": "the process it watches has exited", "written_at": "2020-01-01T00:00:00-05:00",
    }), encoding="utf-8")
    state = _presence(monkeypatch, status)
    assert state["alive"] is False and "exited" in state["reason"]
