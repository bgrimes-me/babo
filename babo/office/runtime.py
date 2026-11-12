"""The runtime adapter — the only module that opens the podman socket.

Two rules shape this file.

**One holder (S-01 / D-10).** Under the desk's ruling the product runs as two
containers: ``babo-core`` is the only one with the runtime socket, and this
module is the only code that opens it. Nothing here is reachable from the
Office interface: no socket path, no container id and no capability argument
crosses that boundary (R10).

**Absence and unreadability are different answers (O-05).**
``inspect_container`` / ``inspect_network`` return ``None`` when the runtime
*answered* that the thing is not there, and raise :class:`RuntimeUnavailableError`
when the runtime could not be read at all. A caller may not catch that error and
continue as if it were an absence; a sweep that cannot read must skip and audit.

The runtime is also not the place a freeze lives. A stand-down is an agent-level
request that preserves work (L-06), so it is a separate, injected
:class:`AgentControl` port — never a signal with a nicer name.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Protocol, cast, runtime_checkable


class RuntimeUnavailableError(RuntimeError):
    """The runtime could not be read. Never to be treated as an absent thing."""


class ControlUnavailableError(RuntimeError):
    """No agent-control port is wired, so a stand-down cannot be requested (L-06)."""


@dataclass(frozen=True)
class Mount:
    source: str
    destination: str
    read_only: bool = True


@dataclass(frozen=True)
class ContainerFacts:
    name: str
    state: str  # running | paused | created | exited | absent (absent is None, not this)
    image: str = ""
    mounts: tuple[Mount, ...] = ()
    cap_add: tuple[str, ...] = ()
    cap_drop: tuple[str, ...] = ()
    network_mode: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    started_at: str = ""
    ip: str = ""

    @property
    def alive(self) -> bool:
        return self.state == "running"


@dataclass(frozen=True)
class NetworkFacts:
    name: str
    subnet: str = ""
    gateway: str = ""
    driver: str = ""
    containers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class LaunchSpec:
    """What to run. A renderer produces it; the core passes it through."""

    name: str
    image: str
    env: dict[str, str] = field(default_factory=dict)
    mounts: tuple[Mount, ...] = ()
    network: str = ""
    network_mode: str = ""
    cap_add: tuple[str, ...] = ()
    cap_drop: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)
    command: tuple[str, ...] = ()


class AgentControl(Protocol):
    """The agent-side handshake that saves work before a pod is released."""

    def request_standdown(self, handle: str) -> None:
        """Ask the agent to save and stand down. Raising means the request failed."""
        ...

    def request_rescue(self, handle: str) -> None:
        """Ask the agent to save its workspace for a rescue. Raising means it failed."""
        ...


@runtime_checkable
class Runtime(Protocol):
    def inspect_container(self, name: str) -> ContainerFacts | None: ...

    def inspect_network(self, name: str) -> NetworkFacts | None: ...

    def list_agents(self) -> list[ContainerFacts]: ...

    def run(self, spec: LaunchSpec) -> ContainerFacts: ...

    def remove(self, name: str) -> None: ...


class UnwiredAgentControl:
    """The default port: honest, and it refuses rather than pretending."""

    def request_standdown(self, handle: str) -> None:
        raise ControlUnavailableError(
            f"no stand-down handshake is configured for {handle}; refusing to kill the pod "
            "(a freeze preserves work, L-06)"
        )

    def request_rescue(self, handle: str) -> None:
        raise ControlUnavailableError(
            f"no rescue handshake is configured for {handle}; refusing to tear the pod down blind"
        )


class PodmanRuntime:
    """``podman --remote`` over the socket inside ``babo-core``.

    The socket URL is configuration of this adapter, never an argument the
    Office surface can set.
    """

    ABSENT = ("no such object", "does not exist", "no such container", "no such network")

    def __init__(self, url: str | None = None, *, timeout: float = 30.0) -> None:
        self.url = url or os.environ.get("BABO_PODMAN_URL", "unix:///run/podman/podman.sock")
        self.timeout = timeout

    def _call(self, *args: str, what: str) -> str:
        cmd = ["podman", "--remote", "--url", self.url, *args]
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, check=False)
        except FileNotFoundError as exc:
            raise RuntimeUnavailableError(f"podman is not installed ({what})") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeUnavailableError(f"podman {what} timed out") from exc
        if done.returncode != 0:
            raise RuntimeUnavailableError(f"podman {what} failed (rc={done.returncode}): {done.stderr.strip()[:400]}")
        return done.stdout

    def _call_maybe_absent(self, *args: str, what: str) -> str | None:
        cmd = ["podman", "--remote", "--url", self.url, *args]
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, check=False)
        except FileNotFoundError as exc:
            raise RuntimeUnavailableError(f"podman is not installed ({what})") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeUnavailableError(f"podman {what} timed out") from exc
        if done.returncode == 0:
            return done.stdout
        text = (done.stderr or "").lower()
        if any(marker in text for marker in self.ABSENT):
            return None
        raise RuntimeUnavailableError(f"podman {what} failed (rc={done.returncode}): {done.stderr.strip()[:400]}")

    # -- reads ----------------------------------------------------------
    def inspect_container(self, name: str) -> ContainerFacts | None:
        out = self._call_maybe_absent("inspect", "--format", "{{json .}}", name, what="inspect container")
        if out is None or not out.strip():
            return None
        return container_facts(json.loads(out))

    def inspect_network(self, name: str) -> NetworkFacts | None:
        out = self._call_maybe_absent("network", "inspect", "--format", "{{json .}}", name,
                                      what="inspect network")
        if out is None or not out.strip():
            return None
        return network_facts(json.loads(out))

    def list_agents(self) -> list[ContainerFacts]:
        names = self._call("ps", "-a", "--filter", "label=babo.managed", "--format", "{{.Names}}",
                           what="list agents").split()
        facts: list[ContainerFacts] = []
        for name in names:
            found = self.inspect_container(name)
            if found is not None:
                facts.append(found)
        return facts

    # -- writes ---------------------------------------------------------
    def run(self, spec: LaunchSpec) -> ContainerFacts:
        args = ["run", "-d", "--name", spec.name]
        for key, value in spec.env.items():
            args += ["-e", f"{key}={value}"]
        for mount in spec.mounts:
            args += ["-v", f"{mount.source}:{mount.destination}{':ro,Z' if mount.read_only else ''}"]
        if spec.network:
            args += ["--network", spec.network]
        if spec.network_mode:
            args += ["--network", spec.network_mode]
        for cap in spec.cap_add:
            args += ["--cap-add", cap]
        for cap in spec.cap_drop:
            args += ["--cap-drop", cap]
        for key, value in spec.labels.items():
            args += ["--label", f"{key}={value}"]
        args.append(spec.image)
        args.extend(spec.command)
        self._call(*args, what="run")
        found = self.inspect_container(spec.name)
        if found is None:  # pragma: no cover - the runtime said it ran
            raise RuntimeUnavailableError(f"container {spec.name} vanished right after run")
        return found

    def remove(self, name: str) -> None:
        out = self._call_maybe_absent("rm", "-f", name, what="remove container")
        del out  # absence is fine: the pod is gone either way


def _obj(value: object) -> dict[str, Any]:
    """A JSON object from podman, or an empty one. Narrowed, not trusted."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _seq(value: object) -> list[Any]:
    """A JSON array from podman, or an empty one."""
    return cast("list[Any]", value) if isinstance(value, list) else []


def container_facts(payload: dict[str, Any]) -> ContainerFacts:
    config = _obj(payload.get("Config"))
    host = _obj(payload.get("HostConfig"))
    state_obj = _obj(payload.get("State"))
    state = str(state_obj.get("Status") or "")
    networks = _obj(_obj(payload.get("NetworkSettings")).get("Networks"))
    ip = ""
    for entry_value in networks.values():
        entry = _obj(entry_value)
        if entry.get("IPAddress"):
            ip = str(entry["IPAddress"])
            break
    mounts = tuple(
        Mount(
            source=str(item.get("Source") or ""),
            destination=str(item.get("Destination") or ""),
            read_only=not bool(item.get("RW", False)),
        )
        for item in (_obj(raw) for raw in _seq(payload.get("Mounts")))
    )
    return ContainerFacts(
        name=str(payload.get("Name") or "").lstrip("/"),
        state=state,
        image=str(config.get("Image") or ""),
        mounts=mounts,
        cap_add=tuple(str(cap) for cap in _seq(host.get("CapAdd"))),
        cap_drop=tuple(str(cap) for cap in _seq(host.get("CapDrop"))),
        network_mode=str(host.get("NetworkMode") or ""),
        labels={str(key): str(value) for key, value in _obj(config.get("Labels")).items()},
        started_at=str(state_obj.get("StartedAt") or ""),
        ip=ip,
    )


def network_facts(payload: dict[str, Any]) -> NetworkFacts:
    subnets = _seq(payload.get("subnets"))
    first = _obj(subnets[0]) if subnets else {}
    containers = tuple(
        (str(name), str(_obj(entry).get("ipv4_address") or ""))
        for name, entry in _obj(payload.get("containers")).items()
    )
    return NetworkFacts(
        name=str(payload.get("name") or ""),
        subnet=str(first.get("subnet") or ""),
        gateway=str(first.get("gateway") or ""),
        driver=str(payload.get("driver") or ""),
        containers=containers,
    )
