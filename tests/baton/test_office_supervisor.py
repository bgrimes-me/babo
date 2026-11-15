"""The pod's PID 1: relay first, and the container dies with either child.

Rows, in the order the rule reads:

- the relay is asked before the agent starts, and a relay that never answers means
  the agent never runs;
- a relay that cannot bind fails the pod rather than half-starting it;
- a relay that dies takes the container with it, and stops the agent;
- an agent that dies first takes the container with it too, with its own status;
- a SIGTERM to the supervisor stops both children and exits 143.

These use real processes, because the thing being tested is process lifecycle and
a fake would only test the fake.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from babo.office import supervisor

# On this host `PYTHON` is empty under `python3 -m pytest`; a row that
# spawns a child needs an interpreter it can actually exec.
PYTHON = shutil.which("python3") or "python3" 


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _relay_script(port: int, *, exit_after: float | None = None,
                  exit_code: int = 0) -> list[str]:
    """A relay that answers /health, optionally dying of its own accord afterwards."""
    body = (
        "import http.server, threading, time, os\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200); self.send_header('Content-Length','2');"
        " self.end_headers(); self.wfile.write(b'{}')\n"
        "    def log_message(self, *a): pass\n"
        f"server = http.server.HTTPServer(('127.0.0.1', {port}), H)\n"
    )
    if exit_after is not None:
        body += (
            f"threading.Timer({exit_after}, lambda: os._exit({exit_code})).start()\n"
        )
    body += "server.serve_forever()\n"
    return [PYTHON, "-c", body]


def _sleeper(seconds: float = 30.0) -> list[str]:
    return [PYTHON, "-c", f"import time; time.sleep({seconds})"]


def _heartbeat(path: Path, interval: float = 0.05) -> list[str]:
    return [
        PYTHON, "-c",
        f"import time\n"
        f"while True:\n"
        f"    open({str(path)!r}, 'a').write('.')\n"
        f"    time.sleep({interval})\n",
    ]


# -- the relay is asked first -------------------------------------------------
def test_a_relay_that_never_answers_means_the_agent_never_starts(tmp_path: Path) -> None:
    marker = tmp_path / "agent-started"
    started = [PYTHON, "-c", f"open({str(marker)!r}, 'w').write('x')"]
    status = supervisor.run(
        _sleeper(), started, address="127.0.0.1", port=_free_port(), probe_timeout_s=0.3,
    )
    assert status == 1
    assert not marker.exists(), "the agent must not run behind a relay that never answered"


def test_a_relay_that_cannot_bind_fails_the_pod(tmp_path: Path) -> None:
    marker = tmp_path / "agent-started"
    started = [PYTHON, "-c", f"open({str(marker)!r}, 'w').write('x')"]
    status = supervisor.run(
        [PYTHON, "-c", "raise SystemExit(3)"], started,
        address="127.0.0.1", port=_free_port(), probe_timeout_s=2.0,
    )
    assert status == 1
    assert not marker.exists()


# -- either death ends the pod ------------------------------------------------
def test_a_relay_that_dies_takes_the_container_and_the_agent_with_it(tmp_path: Path) -> None:
    port = _free_port()
    beats = tmp_path / "agent-alive"
    status = supervisor.run(
        _relay_script(port, exit_after=0.8), _heartbeat(beats),
        address="127.0.0.1", port=port,
    )
    assert status == 1, "a clean child exit is still the end of the pod"
    size = beats.stat().st_size
    time.sleep(0.4)
    assert beats.stat().st_size == size, "the agent must be stopped, not orphaned"


def test_an_agent_that_dies_first_takes_the_container_with_it(tmp_path: Path) -> None:
    port = _free_port()
    status = supervisor.run(
        _relay_script(port), [PYTHON, "-c", "raise SystemExit(7)"],
        address="127.0.0.1", port=port,
    )
    assert status == 7, "the child's own status is the container's"


# -- and a signal stops both ---------------------------------------------------
def test_sigterm_stops_both_children_and_exits_143(tmp_path: Path) -> None:
    port = _free_port()
    beats = tmp_path / "agent-alive"
    env = dict(os.environ)
    env["BABO_OFFICE_BIND"] = "127.0.0.1"
    env["BABO_OFFICE_PORT"] = str(port)
    # `shlex.join` so a `-c` script with quotes and newlines survives the round
    # trip through the supervisor's own `shlex.split`.
    env["BABO_OFFICE_RELAY_CMD"] = shlex.join(_relay_script(port))
    env["BABO_AGENT_CMD"] = shlex.join(_heartbeat(beats))
    process = subprocess.Popen(
        [PYTHON, "-m", "babo.office.supervisor"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 20
        while not beats.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert beats.exists(), "the agent should have started once the relay answered"
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=20) == 143
    finally:
        if process.poll() is None:  # pragma: no cover - only on a wedged run
            process.kill()
            process.wait()


def test_the_two_commands_come_from_the_environment_and_have_no_empty_default() -> None:
    relay, agent = supervisor.commands({"BABO_OFFICE_RELAY_CMD": "relay --flag",
                                       "BABO_AGENT_CMD": "agent --flag"})
    assert list(relay) == ["relay", "--flag"] and list(agent) == ["agent", "--flag"]
    with pytest.raises(SystemExit):
        supervisor.commands({"BABO_OFFICE_RELAY_CMD": "  "})
