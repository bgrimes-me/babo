"""Who lives in the town.

``state/registry.json`` is the record; ``REGISTRY.md`` is rendered from it for
people to read. Only ``founding.py`` adds entries. Everything else may read the
registry, change a resident's chosen name, or mark first-session state.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from filelock import FileLock

from babo.baton.town import (
    RESIDENT_MARKER,
    BatonError,
    Town,
    atomic_write,
    find_town_for_workspace,
)

UNCHOSEN = "unchosen"


@dataclass
class Resident:
    id: str
    office: str
    founded: str
    name: str = UNCHOSEN
    status: str = "active"  # active | resting | moved_away
    first_session_done: bool = False
    last_active: str = ""
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def display(self) -> str:
        return self.id if self.name == UNCHOSEN else f"{self.name} ({self.id})"


class Registry:
    def __init__(self, town: Town):
        self.town = town
        self._lock = FileLock(str(town.locks / "registry.lock"), timeout=30)

    # -- reading ---------------------------------------------------------
    def _load(self) -> dict[str, Resident]:
        path = self.town.registry_json
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        out: dict[str, Resident] = {}
        for item in raw.get("residents", []):
            known = {k: item[k] for k in Resident.__dataclass_fields__ if k in item and k != "extra"}
            known["extra"] = {k: v for k, v in item.items() if k not in Resident.__dataclass_fields__}
            out[item["id"]] = Resident(**known)
        return out

    def all(self) -> list[Resident]:
        return list(self._load().values())

    def get(self, resident_id: str) -> Resident | None:
        return self._load().get(resident_id)

    def resolve(self, who: str) -> Resident | None:
        """Find a resident by id or by chosen name (case-insensitive)."""
        who = who.strip().lower()
        for resident in self._load().values():
            if resident.id == who or (resident.name != UNCHOSEN and resident.name.lower() == who):
                return resident
        return None

    def require_active(self, resident_id: str) -> Resident:
        """The single gate every resident-facing entry point passes through.

        A directory alone is not a resident. It must be in the registry, active,
        and carry the marker founding wrote. Anything else fails closed; this
        function never creates anything.
        """
        resident = self.get(resident_id)
        marker = self.town.resident_dir(resident_id) / RESIDENT_MARKER
        if resident is None or not marker.is_file():
            raise BatonError(
                f"'{resident_id}' is not a resident of this town. Residents are only "
                "created by an approved founding (see `babo review`)."
            )
        if marker.read_text(encoding="utf-8").strip() != resident_id:
            raise BatonError(f"Resident marker for '{resident_id}' does not match its directory.")
        if resident.status != "active":
            raise BatonError(f"Resident '{resident_id}' is {resident.status}.")
        return resident

    # -- writing ---------------------------------------------------------
    def _save(self, residents: dict[str, Resident]) -> None:
        payload = {
            "residents": [
                {**{k: v for k, v in asdict(r).items() if k != "extra"}, **r.extra}
                for r in sorted(residents.values(), key=lambda r: r.founded + r.id)
            ],
        }
        atomic_write(self.town.registry_json, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        atomic_write(self.town.registry_md, render_registry_md(list(residents.values())))

    def _add(self, resident: Resident) -> None:
        """Only for founding.py."""
        with self._lock:
            residents = self._load()
            if resident.id in residents:
                raise BatonError(f"Resident '{resident.id}' already exists")
            residents[resident.id] = resident
            self._save(residents)

    def update(self, resident_id: str, **changes: object) -> Resident:
        allowed = {"name", "status", "first_session_done", "last_active"}
        if not set(changes) <= allowed:
            raise BatonError(f"Registry fields {sorted(set(changes) - allowed)} cannot be changed here")
        with self._lock:
            residents = self._load()
            if resident_id not in residents:
                raise BatonError(f"'{resident_id}' is not a resident of this town")
            resident = residents[resident_id]
            for key, value in changes.items():
                setattr(resident, key, value)
            self._save(residents)
            return resident

    def set_extra(self, resident_id: str, **values: object) -> Resident:
        """Harness bookkeeping kept alongside a resident (first-session key etc.)."""
        with self._lock:
            residents = self._load()
            if resident_id not in residents:
                raise BatonError(f"'{resident_id}' is not a resident of this town")
            residents[resident_id].extra.update(values)
            self._save(residents)
            return residents[resident_id]

    def render(self) -> None:
        with self._lock:
            self._save(self._load())


def render_registry_md(residents: list[Resident]) -> str:
    lines = [
        "# Registry",
        "",
        "Who lives here. Generated from `state/registry.json`; edits here are overwritten.",
        "",
        "| id | name | office | founded | status |",
        "|---|---|---|---|---|",
    ]
    for r in sorted(residents, key=lambda r: r.founded + r.id):
        lines.append(f"| {r.id} | {r.name} | {r.office} | {r.founded[:10]} | {r.status} |")
    return "\n".join(lines) + "\n"


def require_resident_workspace(workspace: Path) -> None:
    """If ``workspace`` sits in a town's residents/, it must be a founded, active
    resident. Ordinary workspaces pass untouched."""
    found = find_town_for_workspace(workspace)
    if found is not None:
        Registry(found[0]).require_active(found[1])
