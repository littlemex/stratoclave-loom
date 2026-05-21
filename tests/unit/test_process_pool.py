"""Tests for the ProcessPool semaphore wrapper."""

from __future__ import annotations

import asyncio

import pytest

from stratoclave_loom.runtime import ProcessPool


async def test_lease_increments_active_within_block() -> None:
    pool = ProcessPool(max_concurrent=2)
    assert pool.active == 0
    async with pool.lease():
        assert pool.active == 1
    assert pool.active == 0


async def test_lease_blocks_when_at_capacity() -> None:
    pool = ProcessPool(max_concurrent=1)

    started = asyncio.Event()
    release = asyncio.Event()

    async def worker() -> None:
        async with pool.lease():
            started.set()
            await release.wait()

    task = asyncio.create_task(worker())
    await started.wait()

    second = asyncio.create_task(_lease_briefly(pool))
    await asyncio.sleep(0.05)
    assert not second.done(), "second lease should be blocked"

    release.set()
    await task
    await second


async def _lease_briefly(pool: ProcessPool) -> None:
    async with pool.lease():
        return None


def test_invalid_capacity_rejected() -> None:
    with pytest.raises(ValueError):
        ProcessPool(max_concurrent=0)
