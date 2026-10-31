"""Writing the town config at install time (``babo init-config``).

Adds Baton's settings to ``~/.babo/config.json`` without disturbing
anything the owner already configured (channels, keys). The original file is
backed up first. Re-running is safe: values the installer owns are refreshed,
everything else is left as it is.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, cast


def _node(data: dict[str, Any], *path: str) -> dict[str, Any]:
    node = data
    for key in path:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = cast(dict[str, Any], child)
    return node


def town_config(
    existing: dict[str, Any],
    *,
    town: str,
    upstream: str,
    model: str,
    n_ctx: int,
    slots: int = 1,
    queue_port: int = 8091,
    bwrap: bool = False,
    box: str = "Agent",
    babo_path: str = "",
    opencode_path: str = "",
) -> dict[str, Any]:
    data = json.loads(json.dumps(existing))
    queue = f"http://127.0.0.1:{queue_port}/v1"
    town_dir = str(Path(town).expanduser())

    provider = _node(data, "providers", "custom")
    provider["apiBase"] = queue
    provider.setdefault("apiKey", "local")

    preset = _node(data, "modelPresets", "town")
    preset.update({"provider": "custom", "model": model, "contextWindowTokens": int(n_ctx)})

    defaults = _node(data, "agents", "defaults")
    defaults["workspace"] = f"{town_dir}/residents/mayor"
    defaults["modelPreset"] = "town"
    _node(defaults, "dream")["enabled"] = False

    tools = _node(data, "tools")
    tools["restrictToWorkspace"] = True
    if bwrap:
        _node(tools, "exec")["sandbox"] = "bwrap"

    baton = _node(data, "baton")
    baton["town"] = town_dir
    baton["box"] = box
    if babo_path:
        baton["baboPath"] = babo_path
    if opencode_path:
        # The full path: Babo's services don't see the PATH of an interactive shell.
        _node(baton, "opencode")["bin"] = opencode_path
    bm = _node(baton, "model")
    bm.update({"endpoint": queue, "upstream": upstream.rstrip("/"), "model": model, "slots": int(slots), "queuePort": int(queue_port)})
    return data


def write_town_config(path: Path, **values: Any) -> tuple[Path, Path | None]:
    path = path.expanduser()
    existing: dict[str, Any] = {}
    backup: Path | None = None
    if path.exists():
        existing = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
        backup = path.with_name(f"config.json.before-baton-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(path, backup)
    data = town_config(existing, **values)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path, backup
