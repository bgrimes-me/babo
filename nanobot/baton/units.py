"""systemd user units for the host. They run nanobot inside the distrobox."""

from __future__ import annotations

import shutil
from importlib.resources import files
from pathlib import Path

from jinja2 import Template

from nanobot.baton.config import BatonConfig

UNITS = ("baton-queue.service", "baton-gateway.service", "baton-night.service", "baton-night.timer")


def render_units(config: BatonConfig, *, distrobox: str = "/usr/bin/distrobox") -> dict[str, str]:
    nanobot = config.nanobot_path or shutil.which("nanobot") or str(Path.home() / ".local" / "bin" / "nanobot")
    values = {"distrobox": distrobox, "box": config.box, "nanobot": nanobot, "time": config.night.time}
    root = files("nanobot.baton") / "templates" / "systemd"
    return {name: Template((root / name).read_text(encoding="utf-8")).render(**values) for name in UNITS}


def write_units(config: BatonConfig, out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, text in render_units(config).items():
        path = out / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written
