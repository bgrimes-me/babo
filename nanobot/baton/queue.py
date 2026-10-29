"""The model queue: every model call in the town goes through one line.

``nanobot baton queue`` runs a small OpenAI-compatible proxy in front of
llama-server. Residents' provider config and Baton's own client point at it.
It lets at most ``baton.model.slots`` requests reach the server at once
(default 1), whichever process they come from, and orders waiting requests by
priority (interactive, then visit, then night), oldest first. Streaming
responses pass straight through.

It also keeps the last request each resident sent, byte for byte, which
``nanobot baton context <resident>`` prints: the exact-context view.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from loguru import logger

from nanobot.baton.config import BatonModelConfig

PRIORITIES = {"interactive": 0, "visit": 1, "night": 2}
_RESIDENT_RE = re.compile(r"You are the resident `([a-z][a-z0-9-]{1,31})`")
_HOP_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "keep-alive", "accept-encoding"}


def resident_of(headers: Any, body: bytes) -> str:
    """Who sent this request: the explicit header, else the bundle's first line."""
    explicit = headers.get("X-Baton-Resident") if headers is not None else None
    if explicit:
        return str(explicit)
    match = _RESIDENT_RE.search(body[:20000].decode("utf-8", "replace"))
    return match.group(1) if match else "unknown"


@dataclass(order=True)
class _Waiter:
    priority: int
    seq: int
    future: asyncio.Future[None] = field(compare=False)


class SlotQueue:
    """A priority semaphore: ``slots`` holders at a time, lowest priority value first."""

    def __init__(self, slots: int):
        self.slots = slots
        self.active = 0
        self.max_active = 0
        self._waiters: list[_Waiter] = []
        self._seq = itertools.count()
        self.served = 0

    @property
    def waiting(self) -> int:
        return sum(1 for w in self._waiters if not w.future.done())

    async def acquire(self, priority: int) -> None:
        if self.active < self.slots and not self._waiters:
            self._take()
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, _Waiter(priority, next(self._seq), future))
        try:
            await future
        except asyncio.CancelledError:
            if future.done() and not future.cancelled():
                self.release()  # we were handed the slot as we were cancelled
            raise

    def _take(self) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)

    def release(self) -> None:
        self.active -= 1
        self.served += 1
        while self._waiters and self.active < self.slots:
            waiter = heapq.heappop(self._waiters)
            if waiter.future.done():
                continue
            self._take()
            waiter.future.set_result(None)


class ModelQueue:
    def __init__(self, config: BatonModelConfig, *, capture_dir: Path | None = None):
        self.config = config
        self.slots = SlotQueue(config.slots)
        self.capture_dir = capture_dir
        self.started = time.time()
        self._session: aiohttp.ClientSession | None = None

    def app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_get("/health", self._health)
        app.router.add_get("/baton/queue", self._status)
        app.router.add_route("*", "/{tail:.*}", self._proxy)
        app.on_cleanup.append(self._close)
        return app

    async def _close(self, _app: web.Application) -> None:
        if self._session is not None:
            await self._session.close()

    def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.request_timeout_s)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _health(self, _req: web.Request) -> web.Response:
        try:
            async with self._client().get(self.config.upstream.rstrip("/") + "/health", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                upstream_ok = resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            upstream_ok = False
        status = 200 if upstream_ok else 503
        return web.json_response({"status": "ok" if upstream_ok else "upstream unavailable", "queue": self._stats()}, status=status)

    def _stats(self) -> dict[str, Any]:
        return {
            "slots": self.slots.slots, "active": self.slots.active, "waiting": self.slots.waiting,
            "served": self.slots.served, "max_active": self.slots.max_active, "uptime_s": int(time.time() - self.started),
        }

    async def _status(self, _req: web.Request) -> web.Response:
        return web.json_response(self._stats())

    def _capture(self, resident: str, path: str, body: bytes) -> None:
        if self.capture_dir is None or not path.endswith("chat/completions") or resident == "unknown":
            return
        try:
            self.capture_dir.mkdir(parents=True, exist_ok=True)
            (self.capture_dir / f"{resident}.json").write_bytes(body)
        except OSError:
            logger.warning("Baton queue: could not capture last request for {}", resident)

    async def _proxy(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        resident = resident_of(request.headers, body)
        priority = PRIORITIES.get(request.headers.get("X-Baton-Priority", "interactive"), 0)
        self._capture(resident, request.path, body)
        url = self.config.upstream.rstrip("/") + request.rel_url.path_qs
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS and not k.lower().startswith("x-baton-")}
        await self.slots.acquire(priority)
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self.slots.release()

        try:
            async with self._client().request(request.method, url, data=body, headers=headers) as upstream:
                response = web.StreamResponse(status=upstream.status)
                for key, value in upstream.headers.items():
                    if key.lower() not in _HOP_HEADERS and key.lower() != "content-encoding":
                        response.headers[key] = value
                await response.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Baton queue: upstream error for {}: {}", resident, exc)
            return web.json_response({"error": {"message": f"model server unavailable: {exc}"}}, status=502)
        finally:
            release()


def last_request(capture_dir: Path, resident: str) -> bytes | None:
    path = capture_dir / f"{resident}.json"
    return path.read_bytes() if path.exists() else None


def pretty(body: bytes) -> str:
    try:
        return json.dumps(json.loads(body), indent=2, ensure_ascii=False)
    except ValueError:
        return body.decode("utf-8", "replace")


async def serve(config: BatonModelConfig, capture_dir: Path) -> None:
    queue = ModelQueue(config, capture_dir=capture_dir)
    runner = web.AppRunner(queue.app())
    await runner.setup()
    site = web.TCPSite(runner, config.queue_host, config.queue_port)
    await site.start()
    logger.info("Baton model queue on {}:{} -> {} ({} slot(s))", config.queue_host, config.queue_port, config.upstream, config.slots)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
