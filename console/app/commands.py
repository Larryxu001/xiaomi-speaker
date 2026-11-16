"""Instant commands from the console to the speaker ("play this station").

The speaker keeps one long-poll request open (GET /api/device/command);
pushing a command answers it at once, so a click on the console page or a
voice tool reaches the speaker in about a second instead of the poller's 20 s.
"""

from __future__ import annotations

import asyncio
import time

_queue: asyncio.Queue | None = None
_last_poll = 0.0


def _q() -> asyncio.Queue:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue(maxsize=20)
    return _queue


def push(command: dict) -> None:
    q = _q()
    while q.full():
        q.get_nowait()
    q.put_nowait(command)


def speaker_online() -> bool:
    return time.time() - _last_poll < 40


async def next_command(wait: float = 25) -> dict | None:
    global _last_poll
    _last_poll = time.time()
    try:
        return await asyncio.wait_for(_q().get(), timeout=wait)
    except asyncio.TimeoutError:
        return None
    finally:
        _last_poll = time.time()
