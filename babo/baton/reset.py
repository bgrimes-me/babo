"""``babo reset``: back everything up, then start again from a fresh install.

What Babo keeps lives in three places: its home (``~/.babo``: the config with
models, keys and channels, the Mayor's chats, logs), the town (residents,
memory, the commons and the town's git history), and the residents' opencode
data (``baton.opencode.data_dir``). A reset packs all three into one archive
first, then removes them, so the next install steps (config, found the town)
start from nothing. ``keep_config`` keeps the config file, so models, keys and
channels don't have to be set up again.

The services on the host should be stopped first; a running resident is
stopped here (without a review: its session is in the backup).
"""

from __future__ import annotations

import shutil
import tarfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from babo.baton.town import BatonError

CONFIG_NAME = "config.json"


@dataclass(frozen=True)
class Plan:
    home: Path
    town: Path
    opencode_data: Path
    keep_config: bool

    def present(self) -> list[Path]:
        return [p for p in (self.home, self.town, self.opencode_data) if p.exists()]


def plan(*, home: Path, town: Path, opencode_data: Path, keep_config: bool) -> Plan:
    home, town, opencode_data = home.expanduser().resolve(), town.expanduser().resolve(), opencode_data.expanduser().resolve()
    user_home = Path.home().resolve()
    for path in (home, town, opencode_data):
        if path in (user_home, Path("/")) or user_home.is_relative_to(path):
            raise BatonError(f"Refusing to reset {path}: it is (or holds) your whole home folder.")
    return Plan(home, town, opencode_data, keep_config)


def _stop_residents(town: Path) -> None:
    from babo.baton import opencode
    from babo.baton.registry import Registry
    from babo.baton.town import Town

    try:
        t = Town.open(town)
    except BatonError:
        return
    for resident in Registry(t).all():
        for session in opencode.sessions(t, resident.id):
            run = opencode.running(t, session)
            if run is not None:
                opencode._stop(run)  # pyright: ignore[reportPrivateUsage]


def backup(p: Plan, folder: Path) -> Path:
    """One archive with everything a reset removes."""
    folder = folder.expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / f"babo-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for path in p.present():
            tar.add(path, arcname=path.name if path != p.opencode_data else "opencode-data")
    return archive


def reset(p: Plan, *, backup_dir: Path) -> tuple[Path, list[str]]:
    """Back up, then remove. Returns the archive and what was removed."""
    if backup_dir.expanduser().resolve().is_relative_to(p.home) or backup_dir.expanduser().resolve().is_relative_to(p.town):
        raise BatonError("Keep the backup outside what is being reset.")
    _stop_residents(p.town)
    archive = backup(p, backup_dir)
    removed: list[str] = []
    for path in (p.town, p.opencode_data):
        if path.exists():
            shutil.rmtree(path)
            removed.append(str(path))
    if p.home.exists():
        config = p.home / CONFIG_NAME
        kept = config.read_bytes() if p.keep_config and config.is_file() else None
        shutil.rmtree(p.home)
        removed.append(str(p.home) + (" (config kept)" if kept is not None else ""))
        if kept is not None:
            p.home.mkdir(parents=True)
            config.write_bytes(kept)
            config.chmod(0o600)
    return archive, removed
