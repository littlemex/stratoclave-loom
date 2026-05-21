"""A small async semaphore-based pool that caps concurrent agent sessions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class ProcessPool:
    """Limits the number of simultaneously open agent sessions.

    The pool does not own the subprocesses; it simply hands out leases.
    Adapters create and tear down their own resources inside the leased
    region.
    """

    def __init__(self, max_concurrent: int = 4) -> None:
        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be a positive integer")
        self._sem = asyncio.Semaphore(max_concurrent)
        self._max = max_concurrent
        self._active = 0

    @property
    def max_concurrent(self) -> int:
        return self._max

    @property
    def active(self) -> int:
        return self._active

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[None]:
        await self._sem.acquire()
        self._active += 1
        try:
            yield
        finally:
            self._active -= 1
            self._sem.release()
