"""Run-scoped adapter between the retained runner and persistent code worker."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from uuid import uuid4

from .backends import BackendConfig
from .remote.runtime import RemotePythonRuntime, launch_remote_python_runtime
from .runner import timestamp


class RemoteRunSession:
    """Expose the worker-owned remote connection to the retained runner."""

    mode = "headful"

    def __init__(self, config: BackendConfig, screenshot_dir: Path, execution: RemotePythonRuntime) -> None:
        self.config = config
        self.screenshot_dir = screenshot_dir
        self.execution = execution
        self.viewport = {"width": execution.width, "height": execution.height}
        self.target_label = f"{execution.backend.upper()} {execution.target.removeprefix(execution.backend + '://')}"
        self.current_url = execution.target

    async def close(self) -> None:
        await self.execution.close()

    async def read_state(self) -> dict[str, str]:
        return {"currentUrl": self.current_url}

    async def capture_screenshot(self, label: str) -> dict[str, object]:
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", label).strip("-").lower() or "frame"
        identifier = f"{safe}-{uuid4()}"
        path = self.screenshot_dir / f"{identifier}.png"
        png = await self.execution.capture_screenshot()
        await asyncio.to_thread(path.write_bytes, png)
        return {
            "id": identifier,
            "label": label,
            "path": path,
            "mimeType": "image/png",
            "capturedAt": timestamp(),
            "currentUrl": self.current_url,
        }

async def launch_remote_session(
    config: BackendConfig,
    screenshot_dir: Path,
    *,
    signal: asyncio.Event | None = None,
) -> RemoteRunSession:
    execution = await launch_remote_python_runtime(config, signal=signal)
    return RemoteRunSession(config, screenshot_dir, execution)
