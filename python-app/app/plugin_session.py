"""One persistent worker-backed session shared by all MCP tools."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .actions import ComputerAction
from .backends import BackendConfig
from .remote.runtime import RemotePythonRuntime, launch_remote_python_runtime

logger = logging.getLogger(__name__)


class PluginSession:
    def __init__(self) -> None:
        self._runtime: RemotePythonRuntime | None = None
        self._config: BackendConfig | None = None
        self._lock = asyncio.Lock()

    def _empty_status(self) -> dict[str, Any]:
        return {
            "connected": False,
            "backend": None,
            "host": None,
            "port": None,
            "width": None,
            "height": None,
        }

    async def status(self) -> dict[str, Any]:
        async with self._lock:
            runtime = self._runtime
            if runtime is None or not runtime.connected:
                return self._empty_status()
            return await runtime.status()

    async def connect(self, config: BackendConfig) -> dict[str, Any]:
        async with self._lock:
            old, self._runtime = self._runtime, None
            self._config = None
            if old is not None:
                await old.close()
            runtime = await launch_remote_python_runtime(config)
            self._runtime = runtime
            self._config = config
            return await runtime.status()

    async def disconnect(self) -> None:
        async with self._lock:
            runtime, self._runtime = self._runtime, None
            self._config = None
            if runtime is not None:
                await runtime.close()

    def _require_runtime(self) -> RemotePythonRuntime:
        if self._runtime is None or not self._runtime.connected:
            raise RuntimeError("remote computer is not connected; call computer_connect first")
        return self._runtime

    async def execute(self, code: str) -> dict[str, Any]:
        async with self._lock:
            return await self._require_runtime().execute(code)

    async def screenshot(self) -> bytes:
        async with self._lock:
            return await self._require_runtime().capture_screenshot()

    async def run_actions(self, actions: list[ComputerAction]) -> bytes:
        async with self._lock:
            payload = [action.model_dump(mode="json") for action in actions]
            return await self._require_runtime().run_actions(payload)
