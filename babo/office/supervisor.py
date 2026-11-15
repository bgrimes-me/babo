"""The pod's PID 1: two children, and the container dies when either does.

The rule this file exists to keep is one sentence: **container alive ⇔ relay AND
agent alive**. Both death paths answer to it. A pod that reports up with no agent
in it is the failure to design against — the Manager would spawn into a pod that
cannot hold a spawn, and `succeeded` would be a lie.

So: the relay starts first and must answer `GET /health` before the agent is
started. A relay that cannot bind therefore fails the pod rather than half-starting
it. After that, whichever child exits first takes the container with it, and the
other is stopped. podman's restart policy is the single retry; there is no ladder
in here.

No judgement, no supervision policy, no restarts. It runs two commands and reports
which one ended the pod.
"""

from __future__ import annotations

import os
import signal
import shlex
import subprocess  # noqa: S404 - this module exists to start processes
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import IO, Sequence

HEALTH_PATH = "/health"
DEFAULT_PROBE_TIMEOUT_S = 30.0
DEFAULT_POLL_INTERVAL_S = 0.1


@dataclass
class Child:
    """One supervised process, with the name the log uses for it."""

    name: str
    argv: Sequence[str]
    process: subprocess.Popen[bytes]

    def stop(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - only on a wedged child
            self.process.kill()
            self.process.wait()


def _spawn(name: str, argv: Sequence[str], log: IO[str]) -> Child:
    log.write(f"[supervisor] starting {name}: {' '.join(argv)}\n")
    log.flush()
    return Child(name=name, argv=argv, process=subprocess.Popen(argv, stdout=log, stderr=log))


def _answers(address: str, port: int, timeout_s: float) -> bool:
    """Does the relay answer its own health route? One attempt, no retry ladder."""
    try:
        with urllib.request.urlopen(
            f"http://{address}:{port}{HEALTH_PATH}", timeout=timeout_s
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_until_healthy(
    address: str,
    port: int,
    *,
    relay: Child,
    timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    clock=time.monotonic,
    sleep=time.sleep,
    probe=_answers,
) -> str | None:
    """Wait for the relay's health answer, or return the sentence that says why not."""
    deadline = clock() + timeout_s
    while clock() < deadline:
        code = relay.process.poll()
        if code is not None:
            return (
                f"the relay exited before it answered {HEALTH_PATH} (status {code}); "
                "a relay that cannot bind fails the pod rather than half-starting it"
            )
        if probe(address, port, 1.0):
            return None
        sleep(poll_interval_s)
    return (
        f"the relay did not answer {HEALTH_PATH} at {address}:{port} within {timeout_s}s; "
        "the agent was not started"
    )


def run(
    relay_argv: Sequence[str],
    agent_argv: Sequence[str],
    *,
    address: str,
    port: int,
    probe_timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
    clock=time.monotonic,
    sleep=time.sleep,
    probe=_answers,
    log: IO[str] = sys.stderr,
) -> int:
    """Start relay, wait for its health, start agent, then exit when either does."""
    relay = _spawn("relay", relay_argv, log)
    failure = wait_until_healthy(
        address, port, relay=relay, timeout_s=probe_timeout_s,
        clock=clock, sleep=sleep, probe=probe,
    )
    if failure is not None:
        relay.stop()
        log.write(f"[supervisor] {failure}\n")
        log.flush()
        return 1

    agent = _spawn("agent", agent_argv, log)
    stopping = {"signal": 0}

    def _stop(signum: int, _frame: object) -> None:
        stopping["signal"] = signum

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, _stop)
        except ValueError:  # pragma: no cover - rows drive run() from a thread
            # Signal handlers belong to the main thread. The rows exercise this
            # function in-process, so installing them is best-effort by design;
            # as PID 1 it always succeeds.
            break

    while True:
        if stopping["signal"]:
            relay.stop()
            agent.stop()
            log.write(f"[supervisor] stopped on signal {stopping['signal']}\n")
            log.flush()
            return 128 + stopping["signal"]
        for child in (relay, agent):
            code = child.process.poll()
            if code is None:
                continue
            other = agent if child is relay else relay
            other.stop()
            # A child that exits at all has ended the pod's purpose, so a clean
            # exit is still a failure here — otherwise podman's restart policy
            # would treat a dead relay as a finished job and leave nothing running.
            status = (code if code > 0 else 1) if code >= 0 else 128 - code
            log.write(
                f"[supervisor] {child.name} exited (status {code}); "
                f"stopped {other.name}; the container exits\n"
            )
            log.flush()
            return status
        sleep(DEFAULT_POLL_INTERVAL_S)


DEFAULT_RELAY_CMD = "python -m babo.office.relay_server"
DEFAULT_AGENT_CMD = "babo gateway --config /run/babo/config.json"


def commands(environ: dict[str, str] | None = None) -> tuple[Sequence[str], Sequence[str]]:
    """The two commands to supervise, from the environment, with no silent emptiness."""
    values = os.environ if environ is None else environ
    relay = shlex.split(values.get("BABO_OFFICE_RELAY_CMD", DEFAULT_RELAY_CMD).strip())
    agent = shlex.split(values.get("BABO_AGENT_CMD", DEFAULT_AGENT_CMD).strip())
    if not relay:
        raise SystemExit("supervisor: BABO_OFFICE_RELAY_CMD is empty; there is nothing to supervise")
    if not agent:
        raise SystemExit("supervisor: BABO_AGENT_CMD is empty; there is nothing to supervise")
    return relay, agent


def main(argv: Sequence[str] | None = None) -> None:
    _ = argv
    bind = os.environ.get("BABO_OFFICE_BIND", "").strip()
    if not bind:
        raise SystemExit(
            "supervisor: BABO_OFFICE_BIND is not set; the health probe has no address to ask. "
            "There is no default bind."
        )
    relay_argv, agent_argv = commands()
    port = int(os.environ.get("BABO_OFFICE_PORT", "9300"))
    raise SystemExit(run(relay_argv, agent_argv, address=bind, port=port))


if __name__ == "__main__":  # pragma: no cover - process entry point
    main()
