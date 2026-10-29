"""The town's settings that are safe to change after install.

`babo settings` and the Settings tab of the web interface's Town page both use
this list, so the two always offer the same choices with the same words. The
values live in the ``baton`` section of Babo's config file; the file is backed
up before each change and everything else in it is left as it is.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import ValidationError

from babo.baton.config import BatonConfig
from babo.baton.town import BatonError

Kind = Literal["int", "time", "text"]


@dataclass(frozen=True)
class Setting:
    key: str  # dotted, snake_case, under `baton`
    label: str
    help: str
    kind: Kind
    applies: str  # when the change takes effect, in plain words


RESTART = "after Babo restarts (babo gateway restart, or the restart button in Settings)"
SETTINGS: tuple[Setting, ...] = (
    Setting("memory.checkpoint_every_tool_calls", "Checkpoint every … tool calls",
            "How often a working resident is asked to write a checkpoint in its own words.", "int", RESTART),
    Setting("memory.commit_gate_after_tool_calls", "Hold commits after … tool calls without a checkpoint",
            "A resident's `git commit` waits until it has written a checkpoint this recently.", "int", RESTART),
    Setting("memory.idle_minutes", "Checkpoint after … quiet minutes",
            "A session that goes quiet this long gets a checkpoint, so nothing is lost.", "int", RESTART),
    Setting("bundle.max_tokens", "Start-of-session context (tokens)",
            "How much a resident sees when a session starts: who it is, its baton, letters and notes.", "int", RESTART),
    Setting("bundle.search_hits", "Notes found at session start",
            "How many related notes are put in front of a resident when a session starts (0–10).", "int", RESTART),
    Setting("night.time", "Night cycle time",
            "When the night runs each day (24-hour, HH:MM, this machine's time).",
            "time", "after the services are rewritten: install steps 05 and the host step"),
    Setting("git_remote", "Push the town to",
            "A git remote name for a backup of the town after each night (empty: no push).", "text", "from the next night"),
)
_BY_KEY = {s.key: s for s in SETTINGS}


def _camel(key: str) -> str:
    head, *rest = key.split("_")
    return head + "".join(part.title() for part in rest)


def _get(cfg: BatonConfig, key: str) -> Any:
    node: Any = cfg
    for part in key.split("."):
        node = getattr(node, part)
    return node


def current(cfg: BatonConfig) -> list[dict[str, Any]]:
    return [{"key": s.key, "label": s.label, "help": s.help, "kind": s.kind, "applies": s.applies,
             "value": _get(cfg, s.key)} for s in SETTINGS]


def _coerce(setting: Setting, raw: object) -> object:
    if setting.kind == "int":
        if isinstance(raw, bool):
            raise BatonError(f"{setting.label}: a whole number, please.")
        try:
            return int(str(raw).strip())
        except ValueError as exc:
            raise BatonError(f"{setting.label}: a whole number, please.") from exc
    return str(raw).strip()


def update(config_path: Path, changes: dict[str, object]) -> list[str]:
    """Apply ``changes`` ({dotted key: value}) to the config file. Returns what changed, in words."""
    unknown = sorted(set(changes) - set(_BY_KEY))
    if unknown:
        raise BatonError(f"Not a town setting: {', '.join(unknown)}. Settings: {', '.join(_BY_KEY)}.")
    path = config_path.expanduser()
    data = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8"))) if path.exists() else {}
    baton: object = data.setdefault("baton", {})
    if not isinstance(baton, dict):
        raise BatonError("The config's baton section is not an object; fix config.json by hand first.")
    proposed = cast(dict[str, Any], json.loads(json.dumps(baton)))
    for key, raw in changes.items():
        node: dict[str, Any] = proposed
        parts = key.split(".")
        for part in parts[:-1]:
            child: object = node.get(_camel(part))
            if not isinstance(child, dict):
                child = {}
                node[_camel(part)] = child
            node = cast(dict[str, Any], child)
        node[_camel(parts[-1])] = _coerce(_BY_KEY[key], raw)
    try:
        before, after = BatonConfig.model_validate(baton), BatonConfig.model_validate(proposed)
    except ValidationError as exc:
        problem = exc.errors()[0]
        where = ".".join(str(p) for p in problem.get("loc", ()))
        raise BatonError(f"That value isn't allowed ({where}: {problem.get('msg', 'invalid')}).") from exc
    said = [f"{_BY_KEY[k].label}: {_get(before, k)} → {_get(after, k)} (takes effect {_BY_KEY[k].applies})"
            for k in changes if _get(before, k) != _get(after, k)]
    if not said:
        return []
    if path.exists():
        shutil.copy2(path, path.with_name(f"{path.name}.before-settings-{datetime.now():%Y%m%d-%H%M%S}"))
    data["baton"] = proposed
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return said
