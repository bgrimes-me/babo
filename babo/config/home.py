"""Default instance paths selected by the global --home option."""

from pathlib import Path

_home_path: Path | None = None


def set_home_path(path: Path | None) -> None:
    """Select the instance root for the current CLI invocation."""
    global _home_path
    _home_path = path.expanduser().resolve() if path is not None else None


def get_selected_home_path() -> Path | None:
    """Return the explicit instance root, if one was selected."""
    return _home_path


def migrate_legacy_home(user_home: Path | None = None) -> Path | None:
    """Move a nanobot-era ``~/.nanobot`` to ``~/.babo`` once, leaving a symlink.

    The symlink keeps absolute paths written into older configs working.
    Returns the new directory when a move happened.
    """
    base = user_home or Path.home()
    new, old = base / ".babo", base / ".nanobot"
    if new.exists() or new.is_symlink() or old.is_symlink() or not old.is_dir():
        return None
    old.rename(new)
    old.symlink_to(".babo")
    return new


def get_home_path() -> Path:
    """Return the selected instance root, or the default ~/.babo directory."""
    return _home_path if _home_path is not None else Path.home() / ".babo"


def get_default_workspace() -> str:
    """Keep the portable default unless an instance home was selected."""
    if _home_path is not None:
        return str(get_home_path() / "workspace")
    return "~/.babo/workspace"
