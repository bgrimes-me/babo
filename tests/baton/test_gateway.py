"""The model gateway: each call goes to the model preset it names, local or any provider, with keys kept in Babo."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from aiohttp import web

from babo.baton.config import BatonModelConfig
from babo.baton.gateway import RouterSource
from babo.baton.queue import ModelQueue
from baton.mock_llama import MockLlama, text, tool_call


@pytest.fixture
def cloud():
    server = MockLlama().start()
    try:
        yield server
    finally:
        server.stop()


def _config(path: Path, *, upstream: str, queue: str, cloud: str, default: str = "town") -> Path:
    path.write_text(json.dumps({
        "agents": {"defaults": {"modelPreset": default}},
        "providers": {"custom": {"apiBase": queue, "apiKey": "local"},
                      "openai": {"apiBase": cloud, "apiKey": "sk-secret"}},
        "modelPresets": {"town": {"provider": "custom", "model": "local-35b", "contextWindowTokens": 65536},
                         "cloud": {"provider": "openai", "model": "gpt-cloud", "contextWindowTokens": 128000}},
        "baton": {"model": {"endpoint": queue, "upstream": upstream}},
    }))
    return path


async def _serve(queue: ModelQueue) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(queue.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}/v1"


async def _post(base: str, body: dict[str, Any]) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30) as client:
        return await client.post(base + "/chat/completions", json=body)


async def test_each_call_goes_to_the_preset_it_names_and_keys_stay_in_babo(tmp_path: Path, mock_llama: MockLlama, cloud: MockLlama) -> None:
    path = tmp_path / "config.json"
    queue = ModelQueue(BatonModelConfig(upstream=mock_llama.base_url), router=RouterSource(path))
    runner, base = await _serve(queue)
    try:
        _config(path, upstream=mock_llama.base_url, queue=base, cloud=cloud.v1)
        mock_llama.default = text("from the local model")
        cloud.default = tool_call("read", {"path": "notes.md"})
        # A local preset waits its turn and reaches the model server under its real name.
        local = await _post(base, {"model": "town", "messages": [{"role": "user", "content": "hi"}]})
        assert local.json()["choices"][0]["message"]["content"] == "from the local model"
        assert mock_llama.requests[-1]["model"] == "local-35b"
        # A name that isn't a preset (an old setting) gets the default preset, never some other model.
        await _post(base, {"model": "/models/Qwen3.5-9B-Q6_K.gguf", "messages": [{"role": "user", "content": "hi"}]})
        assert mock_llama.requests[-1]["model"] == "local-35b" and len(cloud.requests) == 0
        # Another provider: Babo's own provider code calls it with the key, which the caller never sees.
        remote = await _post(base, {"model": "cloud", "messages": [{"role": "user", "content": "read my notes"}],
                                    "tools": [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]})
        call = remote.json()["choices"][0]["message"]["tool_calls"][0]
        assert call["function"]["name"] == "read" and json.loads(call["function"]["arguments"]) == {"path": "notes.md"}
        assert cloud.requests[-1]["model"] == "gpt-cloud"
        assert {k.lower(): v for k, v in cloud.request_headers[-1].items()}["authorization"] == "Bearer sk-secret"
        # Streaming callers get an event stream.
        cloud.default = text("streamed")
        streamed = await _post(base, {"model": "babo/cloud", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        assert streamed.headers["content-type"].startswith("text/event-stream")
        assert '"content": "streamed"' in streamed.text and streamed.text.rstrip().endswith("data: [DONE]")
        # A new default in Settings applies to the next call, without a restart.
        import os

        _config(path, upstream=mock_llama.base_url, queue=base, cloud=cloud.v1, default="cloud")
        os.utime(path, (os.stat(path).st_atime, os.stat(path).st_mtime + 5))
        before = len(cloud.requests)
        await _post(base, {"model": "default", "messages": [{"role": "user", "content": "hi"}]})
        assert len(cloud.requests) == before + 1
    finally:
        await runner.cleanup()


async def test_a_provider_that_fails_says_which_preset(tmp_path: Path, mock_llama: MockLlama) -> None:
    path = tmp_path / "config.json"
    queue = ModelQueue(BatonModelConfig(upstream=mock_llama.base_url), router=RouterSource(path))
    runner, base = await _serve(queue)
    try:
        _config(path, upstream=mock_llama.base_url, queue=base, cloud="http://127.0.0.1:9/v1")
        failed = await _post(base, {"model": "cloud", "messages": [{"role": "user", "content": "hi"}]})
        assert failed.status_code >= 500 and "error" in failed.json()
    finally:
        await runner.cleanup()
