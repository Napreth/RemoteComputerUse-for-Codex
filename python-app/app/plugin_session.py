"""Named persistent remote runtimes owned by one MCP server process."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .actions import ComputerAction
from .backends import BackendConfig
from .remote.runtime import RemotePythonRuntime, RemotePythonStartupCleanupError, launch_remote_python_runtime

_DEFAULT_SESSION = "default"
_SESSION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
logger = logging.getLogger(__name__)


def _observe_task(task: asyncio.Task[Any]) -> None:
    """Retrieve background failures even when the original caller was cancelled."""
    if not task.cancelled():
        task.exception()


@dataclass(slots=True)
class SessionEntry:
    """Own one worker's lifecycle; PluginSessions owns registration and retirement.

    Startup stays owned until adopted or classified by cleanup. A cleanup task
    blocks reuse, including after failure. Only successful finalization removes
    the entry from the registry; cancelling a tool caller transfers no ownership.
    """

    name: str
    config: BackendConfig
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    runtime: RemotePythonRuntime | None = None
    startup: asyncio.Task[RemotePythonRuntime] | None = None
    cleanup: asyncio.Task[None] | None = None
    width: int | None = None
    height: int | None = None

    @property
    def needs_cleanup(self) -> bool:
        return self.runtime is not None or self.startup is not None or self.cleanup is not None

    @property
    def connected(self) -> bool:
        return self.startup is None and self.cleanup is None and self.runtime is not None and self.runtime.connected

    def start(self, config: BackendConfig) -> None:
        """Begin startup while the caller holds this entry's lock."""
        self.config = config
        self.width = None
        self.height = None
        self.startup = asyncio.create_task(launch_remote_python_runtime(config))
        self.startup.add_done_callback(_observe_task)

    async def finish_startup(self) -> None:
        if self.startup is not None:
            # Keep even a failed task until cleanup classifies its failure.
            # Shielding prevents a cancelled caller from losing the worker.
            self.runtime = await asyncio.shield(self.startup)
            self.startup = None

    async def close_runtime(self) -> None:
        """Resolve startup ownership, then close the worker before retirement."""
        try:
            await self.finish_startup()
        except RemotePythonStartupCleanupError:
            # Launch could not confirm cleanup: the failed finalizer must keep
            # the registry slot blocked, just as a runtime.close failure does.
            raise
        except asyncio.CancelledError:
            finalizer = asyncio.current_task()
            assert finalizer is not None
            if (
                finalizer.cancelling()
                or self.startup is None
                or not self.startup.cancelled()
                or self.runtime is not None
            ):
                raise
            # Launch only finishes cancelled after completing its worker cleanup.
            # Cancellation of the finalizer itself cannot establish that fact.
            self.startup = None
        except Exception:  # noqa: BLE001 - launch already cleaned failed startups
            self.startup = None
        if self.runtime is not None:
            await self.runtime.close()
        self.runtime = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "session": self.name,
            "connected": self.connected,
            "backend": self.config.kind,
            "host": self.config.host,
            "port": self.config.port,
            "width": self.width,
            "height": self.height,
            "busy": self.lock.locked() or any(
                task is not None and not task.done() for task in (self.startup, self.cleanup)
            ),
        }


class PluginSessions:
    """Route tools to independently serialized, named persistent runtimes."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionEntry] = {}
        self._registry_lock = asyncio.Lock()

    @staticmethod
    def _empty_status(name: str | None = None) -> dict[str, Any]:
        status: dict[str, Any] = {
            "connected": False,
            "backend": None,
            "host": None,
            "port": None,
            "width": None,
            "height": None,
        }
        if name is not None:
            status["session"] = name
        return status

    @staticmethod
    def _validate_name(name: str) -> str:
        if not _SESSION_NAME.fullmatch(name):
            raise ValueError(
                "session name must be 1-64 characters and contain only letters, numbers, '.', '_', or '-'"
            )
        return name

    async def _select_connected(
        self,
        requested: str | None,
        *,
        allow_missing: bool = False,
    ) -> SessionEntry | None:
        if requested is not None:
            name = self._validate_name(requested)
            async with self._registry_lock:
                entry = self._sessions.get(name)
            if entry is None or not entry.connected:
                if allow_missing:
                    return None
                raise RuntimeError(f"remote session '{name}' is not connected")
            return entry

        async with self._registry_lock:
            entries = tuple(entry for entry in self._sessions.values() if entry.connected)
        if not entries:
            if allow_missing:
                return None
            raise RuntimeError("remote computer is not connected; call computer_connect first")
        if len(entries) > 1:
            raise RuntimeError("multiple remote sessions are connected; specify session")
        return entries[0]

    async def _reserve_anonymous(self, config: BackendConfig) -> SessionEntry:
        """Claim the only anonymous slot and its lock in one registry turn."""
        async with self._registry_lock:
            if self._sessions:
                raise RuntimeError(
                    "a remote session is already registered; specify name when connecting a computer"
                )
            entry = SessionEntry(name=_DEFAULT_SESSION, config=config)
            # A named connect or disconnect can reach this entry immediately
            # after registration. Hold its lock until this first launch ends.
            await entry.lock.acquire()
            self._sessions[_DEFAULT_SESSION] = entry
            return entry

    @staticmethod
    def _require_runtime(entry: SessionEntry) -> RemotePythonRuntime:
        runtime = entry.runtime
        if runtime is None or not entry.connected:
            raise RuntimeError(f"remote session '{entry.name}' is not connected")
        return runtime

    async def status(self, session: str | None = None) -> dict[str, Any]:
        if session is not None:
            name = self._validate_name(session)
            async with self._registry_lock:
                entry = self._sessions.get(name)
            if entry is None:
                return self._empty_status(name)
        else:
            entry = await self._select_connected(None, allow_missing=True)
            if entry is None:
                return self._empty_status()
        async with entry.lock:
            runtime = entry.runtime
            if runtime is None or not entry.connected:
                status = entry.snapshot()
                status.pop("busy")
                return status
            status = await runtime.status()
            return {"session": entry.name, **status}

    async def list_sessions(self) -> list[dict[str, Any]]:
        async with self._registry_lock:
            return [self._sessions[name].snapshot() for name in sorted(self._sessions)]

    async def connect(self, config: BackendConfig, name: str | None = None) -> dict[str, Any]:
        if name is None:
            entry = await self._reserve_anonymous(config)
            try:
                return await self._start_entry(entry, config)
            finally:
                entry.lock.release()
        selected = self._validate_name(name)
        while True:
            async with self._registry_lock:
                current = self._sessions.get(selected)
                if current is None:
                    current = SessionEntry(name=selected, config=config)
                    self._sessions[selected] = current
                entry = current
            async with entry.lock:
                async with self._registry_lock:
                    if self._sessions.get(selected) is not entry:
                        # A disconnect completed while we waited. Resolve the
                        # current entry and its lock before attempting a launch.
                        continue
                if entry.needs_cleanup:
                    await self._close_entry(entry)
                    # Finalization retires the entire entry. Resolve the current
                    # registry entry and lock before starting its replacement.
                    continue
                return await self._start_entry(entry, config)

    async def _start_entry(self, entry: SessionEntry, config: BackendConfig) -> dict[str, Any]:
        try:
            entry.start(config)
            await entry.finish_startup()
            assert entry.runtime is not None
            status = await entry.runtime.status()
            entry.width = int(status["width"])
            entry.height = int(status["height"])
        except BaseException as start_error:
            try:
                await self._close_entry(entry)
            except asyncio.CancelledError:
                # The finalizer still owns cleanup and registry removal.
                pass
            except Exception as cleanup_error:
                if isinstance(start_error, asyncio.CancelledError):
                    # The caller was cancelled, but the finalizer retains its
                    # failed tombstone for a subsequent reconnect to report.
                    logger.warning("Remote runtime cleanup after cancelled connect failed", exc_info=True)
                    raise start_error
                if cleanup_error is start_error:
                    raise
                # A failed finalizer leaves this name blocked; report that
                # recovery condition on the first connect attempt.
                raise cleanup_error from start_error
            raise
        return {"session": entry.name, **status}

    async def _close_entry(self, entry: SessionEntry) -> None:
        """Retire the entry independently of the lifetime of its tool caller."""
        if entry.cleanup is None:
            entry.cleanup = asyncio.create_task(self._finalize_entry(entry))
            entry.cleanup.add_done_callback(_observe_task)
        await asyncio.shield(entry.cleanup)

    async def _finalize_entry(self, entry: SessionEntry) -> None:
        # This task owns both worker cleanup and conditional registry removal.
        # On failure it deliberately leaves entry.cleanup and the name blocked.
        await entry.close_runtime()
        async with self._registry_lock:
            if self._sessions.get(entry.name) is entry:
                self._sessions.pop(entry.name)
            entry.cleanup = None

    async def disconnect(self, session: str | None = None) -> str:
        if session is not None:
            name = self._validate_name(session)
            async with self._registry_lock:
                entry = self._sessions.get(name)
            if entry is None:
                raise RuntimeError(f"remote session '{name}' is not registered")
        else:
            entry = await self._select_connected(None, allow_missing=True)
            if entry is None:
                async with self._registry_lock:
                    registered = tuple(self._sessions.values())
                if len(registered) != 1:
                    raise RuntimeError("remote computer is not connected; specify session")
                entry = registered[0]
            name = entry.name
        async with entry.lock:
            await self._close_entry(entry)
        return name

    async def execute(self, code: str, session: str | None = None) -> dict[str, Any]:
        entry = await self._select_connected(session)
        assert entry is not None
        async with entry.lock:
            return await self._require_runtime(entry).execute(code)

    async def screenshot(self, session: str | None = None) -> bytes:
        entry = await self._select_connected(session)
        assert entry is not None
        async with entry.lock:
            return await self._require_runtime(entry).capture_screenshot()

    async def run_actions(
        self,
        actions: list[ComputerAction],
        session: str | None = None,
    ) -> bytes:
        entry = await self._select_connected(session)
        assert entry is not None
        async with entry.lock:
            payload = [action.model_dump(mode="json") for action in actions]
            return await self._require_runtime(entry).run_actions(payload)
