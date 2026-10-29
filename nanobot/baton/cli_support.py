"""``--as <resident>``: pick which resident a command runs as.

``nanobot agent --as archivist`` becomes ``nanobot agent --workspace
<town>/residents/archivist`` before nanobot parses its arguments. The resident
must already be founded; this never creates one.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _option(args: list[str], *names: str) -> tuple[str | None, int, int]:
    """Return (value, start, length) of the first ``--name value`` / ``--name=value``."""
    for i, arg in enumerate(args):
        for name in names:
            if arg == name and i + 1 < len(args):
                return args[i + 1], i, 2
            if arg.startswith(name + "="):
                return arg.split("=", 1)[1], i, 1
    return None, -1, 0


_RESIDENT_COMMANDS = ("agent", "gateway", "webui", "serve", "status")


def has_as(args: list[str]) -> bool:
    """``--as`` on a command that runs a resident (``nanobot baton ...`` handles its own)."""
    if not any(a == "--as" or a.startswith("--as=") for a in args):
        return False
    rest = [a for i, a in enumerate(args) if not (a == "--home" or (i > 0 and args[i - 1] == "--home") or a.startswith("--home="))]
    first = rest[0] if rest else ""
    return first in _RESIDENT_COMMANDS or first.startswith("-")


def rewrite_as_args(args: list[str]) -> list[str]:
    value, start, length = _option(args, "--as")
    if value is None:
        return args
    from nanobot.baton.registry import Registry
    from nanobot.baton.town import BatonError, Town
    from nanobot.config.loader import load_config

    home, _, _ = _option(args, "--home")
    config_arg, _, _ = _option(args, "--config", "-c")
    if config_arg:
        config_path = Path(config_arg).expanduser()
    else:
        config_path = (Path(home).expanduser() if home else Path.home() / ".nanobot") / "config.json"
    try:
        config = load_config(config_path)
        town = Town.open(config.baton.town_path)
        resident = Registry(town).resolve(value)
        if resident is None:
            raise BatonError(f"'{value}' is not a resident of this town.")
        Registry(town).require_active(resident.id)
    except BatonError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    rest = args[:start] + args[start + length:]
    if any(a in ("--workspace", "-w") or a.startswith("--workspace=") for a in rest):
        print("Error: use either --as or --workspace, not both.", file=sys.stderr)
        raise SystemExit(1)
    return [*rest, "--workspace", str(town.resident_dir(resident.id))]
