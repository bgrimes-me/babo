"""Sessions wrap up when they're done and nobody is there; the owner hears about it."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from babo.baton import notices, opencode, views
from babo.baton.town import BatonError, Town


def test_the_door_counts_who_is_there(tmp_path: Path) -> None:
    upstream = socket.socket()
    upstream.bind(("127.0.0.1", 0))
    upstream.listen()
    status = tmp_path / "door.json"
    port = opencode._free_port()  # pyright: ignore[reportPrivateUsage]
    door = subprocess.Popen([sys.executable, "-m", "babo.baton.door", str(port), str(upstream.getsockname()[1]), str(status), str(subprocess.os.getpid())])
    try:
        for _ in range(100):
            if status.exists():
                break
            time.sleep(0.05)
        assert json.loads(status.read_text())["connections"] == 0
        client = socket.create_connection(("127.0.0.1", port))
        served, _ = upstream.accept()
        client.sendall(b"hello")
        assert served.recv(5) == b"hello"  # bytes pass through unchanged
        assert json.loads(status.read_text())["connections"] == 1
        client.close()
        served.close()
        for _ in range(100):
            if json.loads(status.read_text())["connections"] == 0:
                break
            time.sleep(0.05)
        seen = json.loads(status.read_text())
        assert seen["connections"] == 0 and seen["last_seen"]
    finally:
        door.kill()
        upstream.close()


@pytest.mark.parametrize(("state", "connections", "seen", "minutes", "expected"), [
    ("working", 0, "", 600, ""),                                         # never while it works
    ("idle", 0, "", 1, ""),                                              # just finished: a moment's grace
    ("idle", 0, "", 3, "done with its task"),                            # a one-off task nobody opened
    ("idle", 1, "now", 60, ""),                                          # someone has it open
    ("idle", 1, "now", 121, "open, but nothing has happened for a long while"),
    ("idle", 0, "left 5", 5, ""),                                        # left a few minutes ago
    ("idle", 0, "left 16", 16, "done, and everyone has left"),
    ("waiting", 0, "", 600, ""),                                         # waits for the owner's answer
    ("waiting", 0, "", 25 * 60, "it waited a day for an answer"),
])
def test_when_a_session_wraps_up(town: Town, make_config, monkeypatch: pytest.MonkeyPatch,
                                 state: str, connections: int, seen: str, minutes: int, expected: str) -> None:
    cfg = make_config("mayor")
    now = datetime.now().astimezone()
    session = opencode.new_session(town, cfg, "archivist", title="t")
    session.last_active = (now - timedelta(minutes=minutes)).isoformat()
    last_seen = "" if not seen else now.isoformat() if seen == "now" else (now - timedelta(minutes=int(seen.split()[1]))).isoformat()
    monkeypatch.setattr(opencode, "live", lambda *_a, **_k: {"state": state, "activity": "", "questions": []})
    monkeypatch.setattr(opencode, "presence", lambda *_a, **_k: {"connections": connections, "last_seen": last_seen})
    assert opencode.wrap_up_reason(town, cfg, session, now=now) == expected


def test_the_owner_is_told_once_when_a_resident_waits(town: Town, make_config, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config("mayor")
    session = opencode.new_session(town, cfg, "archivist", title="Install the tools")
    question: dict[str, Any] = {"id": "per_1", "action": "shell", "resources": ["npm install"], "message": ""}
    monkeypatch.setattr(opencode, "running", lambda *_a, **_k: object())
    monkeypatch.setattr(opencode, "live", lambda *_a, **_k: {"state": "waiting", "activity": "", "questions": [question]})
    assert opencode.notify_waiting(town) == [f"archivist/{session.id}"]
    assert opencode.notify_waiting(town) == []  # once
    [notice] = notices.recent(town, unread_only=True)
    assert "archivist is waiting for your answer in “Install the tools”: may it shell npm install?" == notice["text"]
    assert notices.mark_read(town) == 1 and notices.recent(town, unread_only=True) == []


def test_a_frozen_session_can_be_archived_and_brought_back(town: Town, make_config, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config("mayor")
    session = opencode.new_session(town, cfg, "archivist", title="Old work")
    assert views.archive_session(town, "archivist", session.id) == "“Old work” is archived."
    assert opencode.overview(town, "archivist") == [] and len(opencode.overview(town, "archivist", archived=True)) == 1
    views.archive_session(town, "archivist", session.id, archived=False)
    assert len(opencode.overview(town, "archivist")) == 1
    monkeypatch.setattr(opencode, "running", lambda *_a, **_k: object())
    with pytest.raises(BatonError, match="Freeze the session first"):
        views.archive_session(town, "archivist", session.id)
