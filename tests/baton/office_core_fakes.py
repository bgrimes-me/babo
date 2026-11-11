"""Deterministic doubles for the native core's tests. A fixture, not a core."""

from __future__ import annotations

from dataclasses import dataclass, field

from babo.office.runtime import AgentControl, ContainerFacts, LaunchSpec, RuntimeUnavailableError


@dataclass
class RecordingRuntime:
    facts: dict[str, ContainerFacts] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    unavailable: str = ""

    def _check(self) -> None:
        if self.unavailable:
            raise RuntimeUnavailableError(self.unavailable)

    def inspect_container(self, name: str) -> ContainerFacts | None:
        self.calls.append(("inspect", name))
        self._check()
        return self.facts.get(name)

    def inspect_network(self, name: str):  # pragma: no cover - not used by the core
        self.calls.append(("inspect_network", name))
        self._check()
        return None

    def list_agents(self) -> list[ContainerFacts]:
        self.calls.append(("list", ""))
        self._check()
        return list(self.facts.values())

    def run(self, spec: LaunchSpec) -> ContainerFacts:
        self.calls.append(("run", spec.name))
        self._check()
        facts = ContainerFacts(name=spec.name, state="running", ip="10.89.0.42",
                               labels=dict(spec.labels))
        self.facts[spec.name] = facts
        return facts

    def remove(self, name: str) -> None:
        self.calls.append(("remove", name))
        self._check()
        self.facts.pop(name, None)


@dataclass
class RecordingControl:
    """A stand-down that does what a compliant agent would: save and go away."""

    runtime: RecordingRuntime
    fail: str = ""
    calls: list[str] = field(default_factory=list)

    def request_standdown(self, handle: str) -> None:
        self.calls.append(f"standdown:{handle}")
        if self.fail:
            raise RuntimeError(self.fail)
        self.runtime.facts.pop(handle, None)

    def request_rescue(self, handle: str) -> None:
        self.calls.append(f"rescue:{handle}")
        if self.fail:
            raise RuntimeError(self.fail)
        self.runtime.facts.pop(handle, None)


@dataclass
class StubbornControl(AgentControl):
    """A stand-down that is asked for and never completes (L-05's timeout case)."""

    calls: list[str] = field(default_factory=list)

    def request_standdown(self, handle: str) -> None:
        self.calls.append(f"standdown:{handle}")

    def request_rescue(self, handle: str) -> None:
        self.calls.append(f"rescue:{handle}")
