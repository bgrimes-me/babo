"""The model gateway: where each model call in the town goes.

Every call from a resident's opencode and from Babo's own memory work comes
to the model queue (``baton.model.endpoint``) in the OpenAI chat format, and
names a **model preset** from Settings › Models (or nothing, for the default
preset). The gateway decides where it goes:

- **local**: the preset's provider is the queue itself, or the local model
  server behind it (``baton.model.upstream``). The call waits for a slot and
  goes straight through, with the preset's real model name.
- **provider**: any other provider (OpenAI, OpenRouter, Ollama elsewhere,
  Anthropic, …). Babo's own provider code makes the call, with the provider's
  key, which stays inside Babo; the answer comes back in the OpenAI format.

A name that isn't a preset (an old setting, a stale config) goes to the
default preset, so nothing ever asks for a model the owner didn't choose.
The owner's config is read again whenever it changes, so a new default or a
new preset in Settings applies to the next call without a restart.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from loguru import logger

if TYPE_CHECKING:
    from babo.config.schema import Config, ModelPresetConfig

DEFAULT = "default"


@dataclass(frozen=True)
class Route:
    preset_name: str
    model: str  # the real model name the preset uses
    local: bool
    preset: ModelPresetConfig


def _base(url: str) -> str:
    url = url.strip().rstrip("/")
    for suffix in ("/v1",):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url.replace("://localhost", "://127.0.0.1")


def preset_names(config: Config) -> list[str]:
    """Every preset that can be chosen: the default and the named ones."""
    return [DEFAULT, *sorted(config.model_presets)]


def default_preset_name(config: Config) -> str:
    return config.agents.defaults.model_preset or DEFAULT


def presets_overview(config: Config) -> list[dict[str, object]]:
    """The presets an agent or a session can be given, for the profile and session menus."""
    default = default_preset_name(config)
    named = [(name, config.model_presets[name].model) for name in sorted(config.model_presets)]
    rows = [{"name": DEFAULT, "model": config.resolve_default_preset().model}] if default == DEFAULT or not named else []
    rows += [{"name": name, "model": model} for name, model in named]
    return [{**row, "default": row["name"] == default} for row in rows]


class Router:
    def __init__(self, config: Config):
        self.config = config

    def _is_local(self, preset: ModelPresetConfig) -> bool:
        try:
            api_base = self.config.get_api_base(preset.model, preset=preset) or ""
        except (ValueError, KeyError):
            return False
        here = {_base(self.config.baton.model.endpoint), _base(self.config.baton.model.upstream)}
        return bool(api_base) and _base(api_base) in here

    def resolve(self, name: str | None) -> Route:
        requested = (name or "").strip()
        if requested.startswith("babo/"):
            requested = requested[len("babo/"):]
        presets = {DEFAULT: self.config.resolve_default_preset(), **self.config.model_presets}
        # "default" (or nothing) means the preset chosen as the default in Settings.
        chosen = default_preset_name(self.config) if requested in ("", DEFAULT) else requested if requested in presets else ""
        if not chosen:
            # A real model name a preset uses (the Mayor's provider sends those), else the default.
            matches = [n for n, p in presets.items() if p.model == requested]
            chosen = matches[0] if matches else default_preset_name(self.config)
        preset = self.config.resolve_preset(chosen)
        return Route(preset_name=chosen, model=preset.model, local=self._is_local(preset), preset=preset)


class RouterSource:
    """The router for the owner's current config, rebuilt when the file changes."""

    def __init__(self, config_path: Path):
        self.path = config_path
        self._stamp: float | None = None
        self._router: Router | None = None

    def get(self) -> Router:
        from babo.config.loader import load_config

        try:
            stamp = self.path.stat().st_mtime
        except OSError:
            stamp = None
        if self._router is None or stamp != self._stamp:
            try:
                self._router = Router(load_config(self.path))
                self._stamp = stamp
            except Exception as exc:  # a half-written config: keep using the last good one
                if self._router is None:
                    raise
                logger.warning("Baton gateway: config {} unreadable, keeping the previous one: {}", self.path, exc)
        return self._router


# -- calls through Babo's own provider code ---------------------------------------

async def call_provider(config: Config, route: Route, body: dict[str, Any]) -> dict[str, Any]:
    """Make an OpenAI-format chat call through Babo's provider for the route's preset."""
    from babo.providers.factory import make_provider

    provider = make_provider(config, preset=route.preset)
    preset = route.preset
    response = await provider.chat(
        messages=cast(list[dict[str, Any]], body.get("messages") or []),
        tools=cast(list[dict[str, Any]] | None, body.get("tools") or None),
        model=route.model,
        max_tokens=int(body.get("max_tokens") or body.get("max_completion_tokens") or preset.max_tokens),
        temperature=float(body["temperature"]) if body.get("temperature") is not None else preset.temperature,
        reasoning_effort=preset.reasoning_effort,
        tool_choice=cast(str | dict[str, Any] | None, body.get("tool_choice")),
    )
    if response.finish_reason == "error":
        raise ProviderCallError(response.content or "the provider returned an error", response.error_status_code or 502)
    message: dict[str, Any] = {"role": "assistant", "content": response.content or ""}
    if response.tool_calls:
        message["tool_calls"] = [call.to_openai_tool_call() for call in response.tool_calls]
    if response.reasoning_content:
        message["reasoning_content"] = response.reasoning_content
    usage = response.usage
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}", "object": "chat.completion", "created": int(time.time()),
        "model": route.preset_name,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if response.tool_calls else (response.finish_reason or "stop")}],
        "usage": {"prompt_tokens": usage.input_tokens if usage else 0, "completion_tokens": usage.output_tokens if usage else 0,
                  "total_tokens": usage.total_tokens if usage else 0},
    }


class ProviderCallError(RuntimeError):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


async def as_stream(completion: dict[str, Any]) -> AsyncIterator[bytes]:
    """A finished completion, sent as an OpenAI-style event stream."""
    choice = cast(dict[str, Any], completion["choices"][0])
    message = cast(dict[str, Any], choice["message"])
    head = {"id": completion["id"], "object": "chat.completion.chunk", "created": completion["created"], "model": completion["model"]}

    def chunk(delta: dict[str, Any], finish: str | None = None, **extra: Any) -> bytes:
        payload = {**head, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()

    yield chunk({"role": "assistant", "content": ""})
    if message.get("reasoning_content"):
        yield chunk({"reasoning_content": message["reasoning_content"]})
    if message.get("content"):
        yield chunk({"content": message["content"]})
    for index, call in enumerate(cast(list[dict[str, Any]], message.get("tool_calls") or [])):
        yield chunk({"tool_calls": [{"index": index, **call}]})
    yield chunk({}, choice["finish_reason"], usage=completion["usage"])
    yield b"data: [DONE]\n\n"
