"""The town's old words, kept working for a while: every one is listed here, and goes in step 23.

Step 15 gave the town its own vocabulary: a newcomer *moves in* (it was "founding
a resident"); the Mayor sends a resident on an *errand* (it was a "session
request"). The old names still work, so nothing breaks while a town (or an
agent's memory, or a script) catches up, but each use says it's going. The
town's own files are migrated once on upgrade instead (``babo tidy``).

Step 23 removes this module, once the owner has confirmed everything works
under the new names.
"""

from __future__ import annotations

from loguru import logger

GOING = "step 23"

TOOLS = {"request_session": "send_errand"}
BOARD_KINDS = {"founding_request": "move_in_request"}
DECISIONS = {"founding": "move_in", "session": "errand"}
API_PATHS = {"/api/town/session-requests": "/api/town/errands"}
GUIDES = {"session-requests": "errands"}

_warned: set[str] = set()


def new_name(table: dict[str, str], name: str, what: str) -> str:
    """The current name for ``name`` (itself when it's current); an old one is logged once, as going."""
    current = table.get(name)
    if current is None:
        return name
    if name not in _warned:
        _warned.add(name)
        logger.warning("Babo: '{}' is the old name of the {} '{}'; it still works until {}.", name, what, current, GOING)
    return current


def tool(name: str) -> str:
    return new_name(TOOLS, name, "tool")


def board_kind(kind: str) -> str:
    return new_name(BOARD_KINDS, kind, "board post kind")


def decision(kind: str) -> str:
    return new_name(DECISIONS, kind, "kind of decision")


def api_path(path: str) -> str:
    return new_name(API_PATHS, path, "API path")
