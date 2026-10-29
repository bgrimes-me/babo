"""Per-resident harness counters (tool calls since the last checkpoint, last
test result). Kept under ``state/runtime/`` — derived, never committed — so the
commit gate and interval trigger survive restarts and are shared by every
process acting for that resident."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from filelock import FileLock

from babo.baton.town import Town, atomic_write


@dataclass
class ResidentCounters:
    calls_since_checkpoint: int = 0
    total_calls: int = 0
    last_test: str | None = None  # pass | fail
    last_checkpoint_at: str = ""
    work_since_checkpoint: bool = False


class CounterStore:
    def __init__(self, town: Town, resident_id: str):
        self.path: Path = town.state / "runtime" / f"{resident_id}.json"
        self._lock = FileLock(str(town.locks / f"counters-{resident_id}.lock"), timeout=30)

    def load(self) -> ResidentCounters:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ResidentCounters()
        known = {k: raw[k] for k in ResidentCounters.__dataclass_fields__ if k in raw}
        return ResidentCounters(**known)

    def save(self, counters: ResidentCounters) -> None:
        atomic_write(self.path, json.dumps(asdict(counters), indent=2) + "\n")

    def update(self, **changes: object) -> ResidentCounters:
        with self._lock:
            counters = self.load()
            for key, value in changes.items():
                setattr(counters, key, value)
            self.save(counters)
            return counters

    def bump(self) -> ResidentCounters:
        with self._lock:
            counters = self.load()
            counters.calls_since_checkpoint += 1
            counters.total_calls += 1
            counters.work_since_checkpoint = True
            self.save(counters)
            return counters
