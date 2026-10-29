"""systemd user units for the host. They run nanobot inside the distrobox."""

from __future__ import annotations

import shutil
from importlib.resources import files
from pathlib import Path

from jinja2 import Template

from nanobot.baton.config import BatonConfig

UNITS = ("baton-queue.service", "baton-gateway.service", "baton-night.service", "baton-night.timer")
BOUND_GATEWAY_UNIT = "baton-gateway@.service"


def render_units(config: BatonConfig, *, distrobox: str = "/usr/bin/distrobox") -> dict[str, str]:
    nanobot = config.nanobot_path or shutil.which("nanobot") or str(Path.home() / ".local" / "bin" / "nanobot")
    generated = str(Path(config.generated_dir).expanduser())
    values = {"distrobox": distrobox, "box": config.box, "nanobot": nanobot, "time": config.night.time, "generated": generated}
    root = files("nanobot.baton") / "templates" / "systemd"
    names = [*UNITS, BOUND_GATEWAY_UNIT] if config.channel_bindings else list(UNITS)
    units = {name: Template((root / name).read_text(encoding="utf-8")).render(**values) for name in names}
    if config.channel_bindings:
        # With bindings, the Mayor's gateway also runs from its generated config.
        units["baton-gateway.service"] = units["baton-gateway.service"].replace(
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
