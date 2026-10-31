"""The door to a resident's opencode: how Babo knows someone is there.

Each running session has one. People reach the session through it (its web
page through *Open*, its terminal interface through ``babo session open``), and
it passes every byte through to the session's opencode unchanged. It counts the
connections that are open, and writes that down, so upkeep can tell whether
someone is watching or working in the session before it wraps it up. Babo's
own calls go straight to opencode and never count.

It runs as its own small process beside opencode, whichever Babo process
started the session, and stops by itself when opencode does.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Door:
    def __init__(self, target_port: int, status: Path):
        self.target_port = target_port
        self.status = status
        self.open = 0
        self.last_seen = ""

    def _write(self) -> None:
        tmp = self.status.with_suffix(".tmp")
        tmp.write_text(json.dumps({"connections": self.open, "last_seen": self.last_seen}), encoding="utf-8")
        tmp.replace(self.status)

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
            except (ConnectionError, OSError):
                pass

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self.target_port)
        except OSError:
            writer.close()
            return
        self.open += 1
        self.last_seen = _now()
        self._write()
        try:
            # A connection is over as soon as either side closes it.
            pipes = [asyncio.ensure_future(self._pipe(reader, up_writer)), asyncio.ensure_future(self._pipe(up_reader, writer))]
            _done, pending = await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
            for pipe in pending:
                pipe.cancel()
            for end in (writer, up_writer):
                try:
                    end.close()
                except (ConnectionError, OSError):
                    pass
        finally:
            self.open -= 1
            self.last_seen = _now()
            self._write()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


async def serve(listen_port: int, target_port: int, status: Path, watch_pid: int) -> None:
    door = Door(target_port, status)
    door._write()  # pyright: ignore[reportPrivateUsage]
    server = await asyncio.start_server(door.handle, "127.0.0.1", listen_port)
    async with server:
        while _alive(watch_pid):
            await asyncio.sleep(2)


def main() -> None:
    listen_port, target_port, status, watch_pid = int(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4])
    asyncio.run(serve(listen_port, target_port, status, watch_pid))


if __name__ == "__main__":
    main()
