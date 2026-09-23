"""Opt-in live smoke test for the configured RDP or VNC backend."""

from __future__ import annotations

import asyncio
import io
import json
import time
from pathlib import Path

from PIL import Image

from app.backends import load_backend_config
from app.computer_session import ComputerSession


async def wait_for_visual_ready(session: ComputerSession, timeout: float = 30.0) -> bytes:
    """Wait past the transient black logon framebuffer before sending input."""
    deadline = time.monotonic() + timeout
    last_frame = b""
    while time.monotonic() < deadline:
        last_frame = await session.screenshot()
        with Image.open(io.BytesIO(last_frame)) as image:
            histogram = image.convert("L").histogram()
        visible_ratio = sum(histogram[16:]) / max(1, sum(histogram))
        if visible_ratio >= 0.05:
            return last_frame
        await asyncio.sleep(0.5)
    raise RuntimeError("remote framebuffer did not become visually ready within 30 seconds")


async def open_run_dialog(session: ComputerSession, backend: str) -> None:
    if backend == "vnc":
        # TightVNC's Windows service does not consistently inject Super_L or
        # Ctrl+Esc. Click Windows 11's taskbar search box for this interop test.
        status = session.status()
        width = int(status["width"])
        height = int(status["height"])
        await session.click(width // 3, height - 24)
        await asyncio.sleep(0.75)
        await session.type_text("run")
        await asyncio.sleep(0.5)
        await session.keypress(["enter"])
    else:
        await session.keypress(["win", "r"])
    await asyncio.sleep(1.0)


async def main() -> None:
    config = load_backend_config()
    output_directory = Path(__file__).parents[2] / "test-artifacts"
    output_directory.mkdir(exist_ok=True)
    session = ComputerSession()
    connected = await session.connect(config)
    try:
        before = await wait_for_visual_ready(session)
        print("live-smoke: framebuffer ready", flush=True)
        before_path = output_directory / f"{config.kind}-before.png"
        before_path.write_bytes(before)

        width = int(connected["width"])
        height = int(connected["height"])
        await session.keypress(["esc"])
        await asyncio.sleep(0.5)
        await session.type_text("remote")
        await session.screenshot()
        print("live-smoke: text input ready", flush=True)
        await session.keypress(["ctrl", "a"])
        await session.screenshot()
        print("live-smoke: chord input ready", flush=True)
        await session.move(2, 2)
        await session.click(2, 2)
        await session.scroll(width // 2, height // 2, 120 if config.kind == "vnc" else 0, 120)
        await session.screenshot()
        print("live-smoke: pointer input ready", flush=True)

        # Exercise named chords and Unicode text in a transient Windows Run box.
        await open_run_dialog(session, config.kind)
        print("live-smoke: Run dialog requested", flush=True)
        await session.type_text(f"{config.kind.upper()} Unicode: 你好")
        await asyncio.sleep(0.5)
        unicode_frame = await session.screenshot()
        unicode_path = output_directory / f"{config.kind}-unicode.png"
        unicode_path.write_bytes(unicode_frame)

        # Create a marker only through remote keyboard input. The caller can
        # independently verify it inside the guest.
        marker = f"remote-computer-use-{config.kind}-{time.time_ns()}-你好"
        command = f"cmd /u /c echo {marker}>%USERPROFILE%\\remote-computer-use-{config.kind}.txt"
        if config.kind == "vnc":
            # Reuse the visible Run dialog. This avoids racing a modal close
            # against a second taskbar click on servers with delayed input.
            await session.keypress(["ctrl", "a"])
            await asyncio.sleep(0.5)
        else:
            await session.keypress(["esc"])
            await asyncio.sleep(0.25)
            await open_run_dialog(session, config.kind)
        await session.type_text(command)
        await asyncio.sleep(0.25)
        await session.keypress(["enter"])
        await asyncio.sleep(3.0 if config.kind == "vnc" else 1.0)

        after = await session.screenshot()
        after_path = output_directory / f"{config.kind}-after.png"
        after_path.write_bytes(after)
        with Image.open(after_path) as image:
            dimensions = image.size
        print(
            json.dumps(
                {
                    "backend": config.kind,
                    "target": f"{config.host}:{config.port}",
                    "dimensions": dimensions,
                    "marker": marker,
                    "screenshots": [str(before_path), str(unicode_path), str(after_path)],
                }
            )
        )
    finally:
        await session.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
