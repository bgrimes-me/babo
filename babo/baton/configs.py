"""Per-resident gateway configs for channel bindings.

By default every channel reaches the Mayor through one gateway, using the owner's
config as is. When ``baton.channelBindings`` binds a channel to another
resident (``{"telegram": "archivist"}``), ``babo configs`` writes one
config per gateway under ``baton.generatedDir/<resident>/config.json``:

- the Mayor's keeps every unbound channel;
- each bound resident's keeps only its own channels;
- each gets its own workspace and its own ports (base port + n).

They are generated from the raw config file, so ``${VAR}`` secret references
stay references. Each gateway runs as ``babo-gateway@<resident>.service``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

from babo.baton.registry import Registry
from babo.baton.town import BatonError, Town

_CHANNEL_SETTINGS = {
    "sendProgress", "sendToolHints", "showReasoning", "showCompactionNotices", "extractDocumentText",
    "sendMaxRetries", "transcriptionProvider", "transcriptionLanguage",
}


def gateway_residents(bindings: dict[str, str]) -> list[str]:
    return ["mayor", *sorted({r for r in bindings.values() if r != "mayor"})]


def generate_configs(base_path: Path, town: Town, bindings: dict[str, str], out_dir: Path) -> dict[str, Path]:
    raw = cast(dict[str, Any], json.loads(base_path.read_text(encoding="utf-8")))
    registry = Registry(town)
    for channel, resident in bindings.items():
        registry.require_active(resident)
        channels = cast(dict[str, Any], raw.get("channels") or {})
        if channel not in channels:
            raise BatonError(f"channelBindings names '{channel}', which is not configured under channels.")
    written: dict[str, Path] = {}
    base_port = int(cast(dict[str, Any], raw.get("gateway") or {}).get("port", 18790))
    for n, resident in enumerate(gateway_residents(bindings)):
        cfg = json.loads(json.dumps(raw))
        cfg.setdefault("agents", {}).setdefault("defaults", {})["workspace"] = str(town.resident_dir(resident))
        cfg.setdefault("gateway", {})["port"] = base_port + n
        channels = cast(dict[str, Any], cfg.get("channels") or {})
        for name, value in list(channels.items()):
            if name in _CHANNEL_SETTINGS or not isinstance(value, dict):
                continue
            owner = bindings.get(name, "mayor")
            if owner != resident:
                cast(dict[str, Any], value)["enabled"] = False
            elif name == "websocket" and n:
                ws = cast(dict[str, Any], value)
                ws["port"] = int(ws.get("port", 8765)) + n
        target = out_dir / resident / "config.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        os.chmod(target, 0o600)
        written[resident] = target
    return written
