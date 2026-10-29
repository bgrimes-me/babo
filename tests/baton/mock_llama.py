"""A scripted OpenAI-compatible server standing in for llama-server in tests.

Runs aiohttp on its own thread and event loop so both sync and async tests can
use it. Each chat completion pops the next scripted reply; replies can be plain
text, tool calls, JSON for schema turns, deliberately malformed output, HTTP
errors, or a callable that inspects the request. It records every request and
the highest number of requests it ever served at once (for the queue tests).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

Reply = dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]]


def text(content: str) -> dict[str, Any]:
    return {"content": content}


def tool_call(name: str, arguments: dict[str, Any] | str) -> dict[str, Any]:
    args = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"tool_calls": [{"name": name, "arguments": args}]}


def schema(obj: dict[str, Any]) -> dict[str, Any]:
    return {"content": json.dumps(obj)}


def malformed(raw: str = "{not json at all") -> dict[str, Any]:
    return {"content": raw}


def http_error(status: int = 500, body: str = "boom") -> dict[str, Any]:
    return {"http_error": status, "body": body}


@dataclass
class MockLlama:
    script: list[Reply] = field(default_factory=list)
    default: Reply = field(default_factory=lambda: text("ok"))
    delay_s: float = 0.0
    requests: list[dict[str, Any]] = field(default_factory=list)
    max_in_flight: int = 0
    _in_flight: int = 0
    port: int = 0
    _loop: asyncio.AbstractEventLoop | None = None
    _thread: threading.Thread | None = None
    _runner: web.AppRunner | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- lifecycle -------------------------------------------------------
    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def v1(self) -> str:
        return self.base_url + "/v1"

    def start(self) -> MockLlama:
        ready = threading.Event()

        def run() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            app = web.Application()
            app.router.add_get("/health", self._health)
            app.router.add_get("/v1/models", self._models)
            app.router.add_get("/props", self._props)
            app.router.add_post("/v1/chat/completions", self._chat)
            app.router.add_post("/chat/completions", self._chat)
            self._runner = web.AppRunner(app)
            loop.run_until_complete(self._runner.setup())
            site = web.TCPSite(self._runner, "127.0.0.1", 0)
            loop.run_until_complete(site.start())
            self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
            ready.set()
            loop.run_forever()
            loop.run_until_complete(self._runner.cleanup())
            loop.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        ready.wait(10)
        return self

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(5)

    def push(self, *replies: Reply) -> None:
        with self._lock:
            self.script.extend(replies)

    # -- handlers --------------------------------------------------------
    async def _health(self, _req: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _models(self, _req: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "mock-model"}]})

    async def _props(self, _req: web.Request) -> web.Response:
        return web.json_response({"total_slots": 1, "default_generation_settings": {"n_ctx": 32768}})

    def _next(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            reply = self.script.pop(0) if self.script else self.default
        return reply(body) if callable(reply) else reply

    async def _chat(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        with self._lock:
            self.requests.append(body)
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            reply = self._next(body)
            if "http_error" in reply:
                return web.Response(status=reply["http_error"], text=reply.get("body", ""))
            if body.get("stream"):
                return await self._stream(request, reply)
            return web.json_response(self._completion(reply))
        finally:
            with self._lock:
                self._in_flight -= 1

    @staticmethod
    def _tool_calls(reply: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
            for c in reply.get("tool_calls", [])
        ]

    def _completion(self, reply: dict[str, Any]) -> dict[str, Any]:
        calls = self._tool_calls(reply)
        message: dict[str, Any] = {"role": "assistant", "content": reply.get("content")}
        if calls:
            message["tool_calls"] = calls
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "mock-model",
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            "timings": {"predicted_per_second": 40.0, "prompt_per_second": 800.0},
        }

    async def _stream(self, request: web.Request, reply: dict[str, Any]) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)

        async def send(obj: dict[str, Any]) -> None:
            await resp.write(f"data: {json.dumps(obj)}\n\n".encode())

        base = {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": int(time.time()), "model": "mock-model"}
        await send({**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
        if reply.get("content"):
            await send({**base, "choices": [{"index": 0, "delta": {"content": reply["content"]}, "finish_reason": None}]})
        calls = self._tool_calls(reply)
        for i, call in enumerate(calls):
            await send({**base, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": i, **call}]}, "finish_reason": None}]})
        await send({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if calls else "stop"}]})
        await send({**base, "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp
