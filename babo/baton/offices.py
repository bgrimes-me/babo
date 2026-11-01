"""Offices: the town's jobs, as templates a newcomer moves into.

Each is a readable file in the town (``offices/<name>.md``): its title, the
profile a newcomer in it starts with, and the rounds it takes part in; the body
is the role, which becomes the newcomer's ROLE.md (theirs to rewrite).

    ---
    title: Reporter
    profile: {web_fetch: allow, read: town}   # beyond (or short of) a new resident's profile
    joins:                                     # steps it adds to rounds when someone moves in
      - round: paper
        duty: Draft the next edition from what happened today.
        material: previous
    ---
    You write the town's paper. ...

An office nobody holds is a **vacancy**: its steps in rounds wait (or are done
by the office a step names in its ``or``) until someone moves in. Babo ships the
archivist and the inspector, both in the night round already; the Mayor holds
the Mayor's office and isn't a template.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from babo.baton.notes import join_frontmatter, split_frontmatter
from babo.baton.registry import Registry
from babo.baton.town import BatonError, Town, atomic_write, slugify

OFFICES_DIR = "offices"
SHIPPED = ("archivist", "inspector")
MAYOR = "mayor"


@dataclass(frozen=True)
class Join:
    round: str
    duty: str
    material: str = "none"
    when: dict[str, Any] = field(default_factory=lambda: cast(dict[str, Any], {}))  # for a round that doesn't exist yet


@dataclass(frozen=True)
class Office:
    name: str
    title: str
    role: str
    profile: dict[str, str] = field(default_factory=lambda: cast(dict[str, str], {}))
    joins: tuple[Join, ...] = ()


def folder(town: Town) -> Path:
    return town.root / OFFICES_DIR


def profile_changes(raw: object) -> dict[str, str]:
    """A profile as an office or a move-in request gives it: capability levels, and read."""
    from babo.baton.profiles import CAPABILITIES, LEVELS, READ_SCOPES

    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise BatonError("a profile is settings like shell: allow")
    keys = {c.key for c in CAPABILITIES}
    out: dict[str, str] = {}
    for key, value in cast(dict[str, object], raw).items():
        value = str(value).strip().lower()
        if key == "read":
            if value not in READ_SCOPES:
                raise BatonError("read is own or town")
        elif key not in keys:
            raise BatonError(f"`{key}` isn't part of a profile (it can be: {', '.join(sorted(keys))}, read)")
        elif value not in LEVELS:
            raise BatonError(f"`{key}` is allow, ask or deny")
        out[str(key)] = value
    return out


def joins_from(raw: object) -> tuple[Join, ...]:
    from babo.baton.rounds import MATERIALS, _when  # pyright: ignore[reportPrivateUsage]

    if raw in (None, ""):
        return ()
    if not isinstance(raw, list):
        raise BatonError("`joins` is a list of rounds, each with a duty")
    out: list[Join] = []
    for item in cast(list[object], raw):
        if not isinstance(item, dict):
            raise BatonError("each of `joins` names a round and a duty")
        j = cast(dict[str, Any], item)
        name, duty, material = slugify(str(j.get("round") or ""), 40), str(j.get("duty") or "").strip(), str(j.get("material") or "none")
        if not name or not duty:
            raise BatonError("each of `joins` names a round and a duty")
        if material not in MATERIALS:
            raise BatonError(f"a step's material is one of: {', '.join(MATERIALS)}")
        out.append(Join(name, duty, material, _when(j["when"]) if j.get("when") else {}))
    return tuple(out)


def parse(name: str, text: str) -> Office:
    meta, body = split_frontmatter(text)
    if not body.strip():
        raise BatonError(f"offices/{name}.md has no role (the text after the frontmatter)")
    return Office(name, str(meta.get("title") or name.replace("-", " ").title()), body.strip(),
                  profile_changes(meta.get("profile")), joins_from(meta.get("joins")))


def load(town: Town) -> tuple[list[Office], dict[str, str]]:
    if not folder(town).exists():
        ship(town)
    out: list[Office] = []
    broken: dict[str, str] = {}
    for path in sorted(folder(town).glob("*.md")):
        try:
            out.append(parse(path.stem, path.read_text(encoding="utf-8")))
        except (OSError, BatonError) as exc:
            broken[path.stem] = str(exc)
    return out, broken


def get(town: Town, name: str) -> Office | None:
    return next((o for o in load(town)[0] if o.name == slugify(name, 28)), None)


def text_of(office: Office) -> str:
    meta: dict[str, Any] = {"title": office.title}
    if office.profile:
        meta["profile"] = dict(office.profile)
    if office.joins:
        meta["joins"] = [{k: v for k, v in asdict(j).items() if v not in ({}, "none") or k in ("round", "duty")} for j in office.joins]
    return join_frontmatter(meta, office.role.rstrip() + "\n")


def write(town: Town, office: Office) -> Path:
    path = folder(town) / f"{office.name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = text_of(office)
    parse(office.name, text)  # it reads
    atomic_write(path, text)
    return path


def ship(town: Town) -> list[str]:
    """The offices Babo ships (vacancies in a new town), from the roles the charter describes."""
    from babo.baton.founding import _write_shipped, template_text  # pyright: ignore[reportPrivateUsage]

    out: list[str] = []
    for name in SHIPPED:
        path = folder(town) / f"{name}.md"
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        role = template_text(f"offices/{name}.md", town.owner_name)
        _write_shipped(town, path, join_frontmatter({"title": name.title()}, role.rstrip() + "\n"))
        out.append(f"{OFFICES_DIR}/{name}.md")
    return out


def holders(town: Town, name: str) -> list[str]:
    return sorted(r.id for r in Registry(town).all() if r.office == name and r.status == "active")


def vacancies(town: Town) -> list[Office]:
    """Offices nobody holds now."""
    return [o for o in load(town)[0] if o.name != MAYOR and not holders(town, o.name)]


def overview(town: Town) -> dict[str, Any]:
    offices, broken = load(town)
    return {"offices": [{**asdict(o), "holders": holders(town, o.name), "vacant": o.name != MAYOR and not holders(town, o.name)}
                        for o in offices], "broken": broken}


# -- joining rounds ----------------------------------------------------------------


def join_rounds(town: Town, office: str, joins: tuple[Join, ...]) -> list[str]:
    """Add an office's steps to the rounds it joins (a round that doesn't exist yet is made, if the join says
    when it runs). A round that already has a step for the office is left as it is."""
    from babo.baton import rounds

    said: list[str] = []
    for j in joins:
        path = rounds.folder(town) / f"{j.round}.md"
        step: dict[str, Any] = {"office": office, "duty": j.duty}
        if j.material != "none":
            step["material"] = j.material
        if not path.exists():
            if not j.when:
                said.append(f"there's no round “{j.round}” to join (and no `when` to start one)")
                continue
            meta: dict[str, Any] = {"title": f"The {j.round.replace('-', ' ')} round", "when": j.when, "steps": [step]}
            text = join_frontmatter(meta, f"Started when the {office} moved in.\n")
            rounds.parse(j.round, text)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, text)
            said.append(f"started the round “{j.round}”")
            continue
        meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
        steps = cast(list[dict[str, Any]], meta.get("steps") or [])
        if any(str(s.get("office")) == office for s in steps):
            said.append(f"“{j.round}” already has a step for the {office}")
            continue
        steps.append(step)
        meta["steps"] = steps
        text = join_frontmatter(meta, body)
        rounds.parse(j.round, text)  # still reads
        atomic_write(path, text)
        said.append(f"joined “{j.round}”")
    return said
