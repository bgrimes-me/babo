"""systemd user units for the host. They run babo inside the distrobox."""

from __future__ import annotations

import shutil
from importlib.resources import files
from pathlib import Path

from jinja2 import Template

from babo.baton.config import BatonConfig

UNITS = ("babo-queue.service", "babo-gateway.service", "babo-night.service", "babo-night.timer")
BOUND_GATEWAY_UNIT = "babo-gateway@.service"


def render_units(config: BatonConfig, *, distrobox: str = "/usr/bin/distrobox") -> dict[str, str]:
    babo = config.babo_path or shutil.which("babo") or str(Path.home() / ".local" / "bin" / "babo")
    generated = str(Path(config.generated_dir).expanduser())
    values = {"distrobox": distrobox, "box": config.box, "babo": babo, "time": config.night.time, "generated": generated}
    root = files("babo.baton") / "templates" / "systemd"
    names = [*UNITS, BOUND_GATEWAY_UNIT] if config.channel_bindings else list(UNITS)
    units = {name: Template((root / name).read_text(encoding="utf-8")).render(**values) for name in names}
    if config.channel_bindings:
        # With bindings, the Mayor's gateway also runs from its generated config.
        units["babo-gateway.service"] = units["babo-gateway.service"].replace(
            "gateway --foreground", f"gateway --foreground --config {generated}/mayor/config.json")
    return units


def write_units(config: BatonConfig, out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, text in render_units(config).items():
        path = out / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written
