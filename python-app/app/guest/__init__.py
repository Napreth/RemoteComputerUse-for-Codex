"""Guest-OS workflows kept separate from RDP and VNC transports."""

from __future__ import annotations

import os

from ..remote.runtime import RemotePythonRuntime


async def open_url(execution: RemotePythonRuntime, url: str) -> bytes:
    guest_os = os.getenv("REMOTE_COMPUTER_GUEST_OS", "").strip().lower()
    if guest_os != "windows":
        raise RuntimeError(
            "lab scenarios require a configured guest URL workflow; "
            "set REMOTE_COMPUTER_GUEST_OS=windows for the supported Windows workflow"
        )
    from .windows import open_url as open_windows_url

    return await open_windows_url(execution, url)


__all__ = ["open_url"]
