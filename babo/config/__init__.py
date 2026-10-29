"""Configuration module for babo."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from babo.config.errors import ConfigIssue, ConfigLoadError
    from babo.config.loader import get_config_path, load_config
    from babo.config.paths import (
        get_cli_history_path,
        get_cron_dir,
        get_data_dir,
        get_legacy_sessions_dir,
        get_logs_dir,
        get_media_dir,
        get_runtime_subdir,
        get_webui_dir,
        get_workspace_path,
        is_default_workspace,
    )
    from babo.config.schema import Config


_EXPORTS = {
    "Config": "babo.config.schema",
    "ConfigIssue": "babo.config.errors",
    "ConfigLoadError": "babo.config.errors",
    "load_config": "babo.config.loader",
    "get_config_path": "babo.config.loader",
    "get_data_dir": "babo.config.paths",
    "get_runtime_subdir": "babo.config.paths",
    "get_media_dir": "babo.config.paths",
    "get_cron_dir": "babo.config.paths",
    "get_logs_dir": "babo.config.paths",
    "get_webui_dir": "babo.config.paths",
    "get_workspace_path": "babo.config.paths",
    "is_default_workspace": "babo.config.paths",
    "get_cli_history_path": "babo.config.paths",
    "get_legacy_sessions_dir": "babo.config.paths",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = [
    "Config",
    "ConfigIssue",
    "ConfigLoadError",
    "load_config",
    "get_config_path",
    "get_data_dir",
    "get_runtime_subdir",
    "get_media_dir",
    "get_cron_dir",
    "get_logs_dir",
    "get_webui_dir",
    "get_workspace_path",
    "is_default_workspace",
    "get_cli_history_path",
    "get_legacy_sessions_dir",
]
