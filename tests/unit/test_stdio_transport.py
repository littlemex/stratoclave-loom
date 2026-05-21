"""Smoke tests for StdioTransport using a tiny Python subprocess as fixture."""

from __future__ import annotations

import sys

import pytest

from stratoclave_loom.core.errors import TransportError
from stratoclave_loom.transport import StdioTransport

ECHO_SCRIPT = (
    "import sys\nfor line in sys.stdin:\n    sys.stdout.write(line)\n    sys.stdout.flush()\n"
)


async def test_send_and_receive_round_trip() -> None:
    transport = StdioTransport(argv=(sys.executable, "-c", ECHO_SCRIPT))
    await transport.start()
    try:
        await transport.send({"hello": "world", "n": 1})
        gen = transport.receive()
        msg = await gen.__anext__()
        assert msg == {"hello": "world", "n": 1}
    finally:
        await transport.close()


async def test_start_twice_raises() -> None:
    transport = StdioTransport(argv=(sys.executable, "-c", "pass"))
    await transport.start()
    try:
        with pytest.raises(TransportError):
            await transport.start()
    finally:
        await transport.close()


def test_argv_must_not_be_empty() -> None:
    with pytest.raises(ValueError):
        StdioTransport(argv=())
