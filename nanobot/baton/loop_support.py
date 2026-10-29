"""The two classes AgentLoop picks at construction time.

``AgentLoop.__init__`` asks this module (one ``# BATON:`` edit) which context
builder and which consolidator to build. For an ordinary workspace it hands
back exactly the classes it was given, so non-town use of nanobot (and
upstream's tests, which patch those names) is unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from nanobot.baton.town import find_town_for_workspace

C = TypeVar("C")
K = TypeVar("K")


def loop_classes(workspace: Path, context_cls: type[C], consolidator_cls: type[K]) -> tuple[type[C], type[K]]:
    found = find_town_for_workspace(workspace)
    if found is None:
        return context_cls, consolidator_cls
    from nanobot.baton.registry import Registry

    # Fail closed before the loop creates anything inside residents/.
    Registry(found[0]).require_active(found[1])
    from nanobot.baton.compaction import BatonConsolidator
    from nanobot.baton.context import BatonContextBuilder

    return BatonContextBuilder, BatonConsolidator  # type: ignore[return-value]
