"""Baton configuration (the ``baton`` section of nanobot's config).

Everything machine-specific lives here. Nothing in Baton hard-codes a port,
path or model name.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from nanobot.config_base import Base

# Tool names each office gets by default. Upstream tools first, then Baton's.
# Keep every list at or under ~12 entries: a 9B model chooses badly from more.
_COMMON_BATON = [
    "search_memory",
    "read_note",
    "remember",
    "revise_self",
    "checkpoint",
    "send_letter",
    "propose_tool",
    "propose_change",
]

DEFAULT_TOOLSETS: dict[str, list[str]] = {
    "mayor": ["read_file", "write_file", "exec", "spawn", *_COMMON_BATON, "visit", "post_board"],
    "archivist": [
        "read_file",
        *_COMMON_BATON,
        "commons_write",
        "commons_supersede",
        "commons_contest",
    ],
    "inspector": ["read_file", *_COMMON_BATON, "file_flag", "review_item"],
    "default": ["read_file", "write_file", "exec", "spawn", *_COMMON_BATON],
}


class BatonModelConfig(Base):
    """Where Baton's own structured (JSON-schema) model calls go."""

    # The model queue's address. Residents' provider config should point here too.
    endpoint: str = "http://127.0.0.1:8091/v1"
    # llama-server itself; only the queue talks to it directly.
    upstream: str = "http://127.0.0.1:8080"
    model: str = "local"
    api_key: str = ""
    # llama-server slots. All model calls share this many lanes, town-wide.
    slots: int = Field(default=1, ge=1)
    queue_host: str = "127.0.0.1"
    queue_port: int = 8091
    request_timeout_s: int = Field(default=600, ge=10)
    schema_max_tokens: int = Field(default=1200, ge=64)
    temperature: float = 0.2


class BatonMemoryConfig(Base):
    """Timing for memory formation. Timing is fixed infrastructure: it can be
    tuned here but there is deliberately no switch that turns it off."""

    checkpoint_every_tool_calls: int = Field(default=15, ge=1)
    # Block `git commit` when the last checkpoint is older than this many tool calls.
    commit_gate_after_tool_calls: int = Field(default=30, ge=1)
    idle_minutes: int = Field(default=20, ge=1)


class BatonBundleConfig(Base):
    """Session start bundle budget (tokens, approximate)."""

    max_tokens: int = Field(default=2200, ge=256)
    search_hits: int = Field(default=3, ge=0, le=10)
    letter_preview_lines: int = Field(default=5, ge=0, le=50)


class BatonNightConfig(Base):
    time: str = Field(default="04:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")  # local time, for the systemd timer
    tool_test_timeout_s: int = Field(default=120, ge=5)
    tool_test_image: str = "docker.io/library/python:3.12-slim"


class BatonConfig(Base):
    """Root of the ``baton`` config section."""

    town: str = "~/baton-town"
    # The search index is derived data and lives outside the town repo.
    index_path: str = "~/.local/share/baton/index.sqlite3"
    # Configs generated per resident (they may contain channel secrets).
    generated_dir: str = "~/.nanobot/baton"
    git_remote: str = ""  # push after the night commit when set (Squid sets this up)
    # Where nanobot runs: the distrobox name, and nanobot's path inside it ("" = find it).
    box: str = "arch"
    nanobot_path: str = ""
    model: BatonModelConfig = Field(default_factory=BatonModelConfig)
    memory: BatonMemoryConfig = Field(default_factory=BatonMemoryConfig)
    bundle: BatonBundleConfig = Field(default_factory=BatonBundleConfig)
    night: BatonNightConfig = Field(default_factory=BatonNightConfig)
    toolsets: dict[str, list[str]] = Field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_TOOLSETS.items()})
    # channel name -> resident id. Unbound channels go to the Mayor.
    channel_bindings: dict[str, str] = Field(default_factory=dict)

    @property
    def town_path(self) -> Path:
        return Path(self.town).expanduser()

    @property
    def index_file(self) -> Path:
        return Path(self.index_path).expanduser()

    @property
    def capture_dir(self) -> Path:
        """Where the model queue keeps each resident's last request (exact-context view)."""
        return self.index_file.parent / "last-request"

    def toolset_for(self, resident_id: str, office: str) -> list[str]:
        for key in (resident_id, office, "default"):
            if key in self.toolsets:
                return list(self.toolsets[key])
        return list(DEFAULT_TOOLSETS["default"])
