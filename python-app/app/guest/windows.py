"""Windows guest workflows expressed through the protocol-neutral Computer API."""

from __future__ import annotations

import asyncio
import io
import os
import time
from typing import Any

from PIL import Image

from ..remote.runtime import RemotePythonRuntime


async def wait_for_visual_ready(
    execution: RemotePythonRuntime,
    timeout: float = 30.0,
) -> bytes:
    """Wait past transient black logon frames before sending guest shortcuts."""
    deadline = time.monotonic() + timeout
    last_frame = b""
    while time.monotonic() < deadline:
        last_frame = await execution.capture_screenshot()
        with Image.open(io.BytesIO(last_frame)) as image:
            histogram = image.convert("L").histogram()
        if sum(histogram[16:]) / max(1, sum(histogram)) >= 0.05:
            return last_frame
        await asyncio.sleep(0.5)
    raise RuntimeError("remote framebuffer did not become visually ready within 30 seconds")


async def open_url(execution: RemotePythonRuntime, url: str) -> bytes:
    return await run_command(execution, f'msedge.exe --no-first-run --no-default-browser-check "{url}"')


def validate_navigation(backend: str) -> None:
    if backend == "vnc" and os.getenv("REMOTE_COMPUTER_VNC_WINDOWS_NAVIGATION") != "win-r":
        raise RuntimeError(
            "VNC Windows navigation is not configured: verify that this server forwards Win+R, "
            "then set REMOTE_COMPUTER_VNC_WINDOWS_NAVIGATION=win-r. No taskbar coordinate fallback is used."
        )


async def run_command(execution: RemotePythonRuntime, command: str) -> bytes:
    validate_navigation(execution.backend)
    await wait_for_visual_ready(execution)
    actions: list[dict[str, Any]] = [
        {"type": "keypress", "keys": ["win", "r"]},
        {"type": "wait", "seconds": 1.0},
        {"type": "type", "text": command},
        {"type": "keypress", "keys": ["enter"]},
        {"type": "wait", "seconds": 3.0},
    ]
    return await execution.run_actions(actions)
