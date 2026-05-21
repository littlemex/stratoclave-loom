"""Asyncio-based stdio JSON-RPC transport for adapters.

This is intentionally minimal. It owns the subprocess lifetime, framing
(newline-delimited JSON), and graceful cancellation. It does **not**
interpret JSON-RPC method semantics; that is the adapter's job.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import AsyncIterator, Mapping
from typing import Any

from stratoclave_loom.config import get_settings
from stratoclave_loom.core.errors import TransportError


class StdioTransport:
    """Manages a subprocess and its stdio streams as newline-delimited JSON."""

    def __init__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if not argv:
            raise ValueError("argv must contain at least the executable")
        self._argv = argv
        self._cwd = cwd
        self._env = dict(env) if env is not None else None
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_buffer: list[str] = []

    @property
    def started(self) -> bool:
        return self._proc is not None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    async def start(self) -> None:
        """Spawn the subprocess. Idempotent: re-calling raises."""
        if self._proc is not None:
            raise TransportError("transport already started")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=self._env if self._env is not None else os.environ.copy(),
            )
        except OSError as exc:
            raise TransportError(f"failed to spawn {self._argv[0]!r}: {exc}") from exc
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name="stratoclave-loom-stderr"
        )

    async def send(self, message: Mapping[str, Any]) -> None:
        proc = self._require_proc()
        if proc.stdin is None:
            raise TransportError("stdin is not available on the spawned process")
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            proc.stdin.write(line.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise TransportError(f"failed to write to subprocess stdin: {exc}") from exc

    async def receive(self) -> AsyncIterator[Mapping[str, Any]]:
        """Yield each JSON message from stdout until EOF.

        Lines that are empty or fail to parse are dropped silently after
        being recorded in the stderr buffer (callers can inspect stderr via
        :meth:`stderr_log`).
        """
        proc = self._require_proc()
        if proc.stdout is None:
            raise TransportError("stdout is not available on the spawned process")
        async for raw in proc.stdout:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                msg = json.loads(stripped)
            except json.JSONDecodeError:
                # Forward malformed lines to the stderr buffer for diagnostics
                # rather than crashing the receive loop. Adapters can opt to
                # surface them as error chunks if needed.
                self._stderr_buffer.append(f"<malformed-stdout> {stripped!r}")
                continue
            if not isinstance(msg, dict):
                self._stderr_buffer.append(f"<non-object-stdout> {stripped!r}")
                continue
            yield msg

    async def cancel(self) -> None:
        """Send SIGINT, then SIGTERM after a grace period, then SIGKILL."""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        grace = get_settings().cancel_grace_ms / 1000.0

        try:
            proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
            return
        except TimeoutError:
            pass

        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
            return
        except TimeoutError:
            pass

        try:
            proc.kill()
        except ProcessLookupError:
            return
        await proc.wait()

    async def close(self) -> None:
        """Close stdio streams and reap the subprocess."""
        proc = self._proc
        if proc is None:
            return
        if proc.stdin is not None and not proc.stdin.is_closing():
            proc.stdin.close()
        if proc.returncode is None:
            await self.cancel()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._proc = None
        self._stderr_task = None

    def stderr_log(self) -> tuple[str, ...]:
        """Snapshot of recent stderr / parse-warning lines."""
        return tuple(self._stderr_buffer)

    def _require_proc(self) -> asyncio.subprocess.Process:
        if self._proc is None:
            raise TransportError("transport has not been started")
        return self._proc

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            async for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if line:
                    self._stderr_buffer.append(line)
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception as exc:  # pragma: no cover - defensive
            self._stderr_buffer.append(f"<stderr-drain-failure> {exc!r}")
