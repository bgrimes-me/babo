"""Runtime log visibility controls shared by CLI commands."""

from loguru import logger

__all__ = ["_set_babo_logs"]


def _set_babo_logs(enabled: bool) -> None:
    if enabled:
        logger.enable("babo")
    else:
        logger.disable("babo")
