"""Restore-or-nothing compaction for residents (filled in by phase 2)."""

from __future__ import annotations

from nanobot.agent.memory import Consolidator


class BatonConsolidator(Consolidator):
    """Consolidator for resident sessions."""
