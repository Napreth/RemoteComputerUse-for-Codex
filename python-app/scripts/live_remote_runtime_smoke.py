"""Opt-in live smoke test for the persistent exec_py-style remote runtime."""

from __future__ import annotations

import asyncio
import base64
import json
import textwrap
import time
from pathlib import Path
from typing import Any

from PIL import Image

from app.backends import load_backend_config
from app.remote.runtime import launch_remote_python_runtime


def save_images(result: dict[str, Any], output_directory: Path, prefix: str) -> list[str]:
    paths: list[str] = []
    for index, item in enumerate(result["output"]):
        if item["type"] != "input_image":
            continue
        path = output_directory / f"{prefix}-{index}.png"
        path.write_bytes(base64.b64decode(item["image_url"].split(",", 1)[1], validate=True))
        with Image.open(path) as image:
            image.verify()
        paths.append(str(path))
    return paths


def validate_result(result: dict[str, Any], completion: str, images: int) -> list[str]:
    text = [item["text"] for item in result["output"] if item["type"] == "input_text"]
    if any("Traceback (most recent call last)" in value or "Error:" in value for value in text):
        raise RuntimeError(f"runtime smoke execution failed: {text}")
    frames = sum(item["type"] == "input_image" for item in result["output"])
    if completion not in text or frames < images:
        raise RuntimeError(f"runtime smoke did not complete ({completion}, {frames} images): {text}")
    return text


def exercise_code(backend: str) -> str:
    horizontal = "computer.hscroll(1)" if backend == "vnc" else "pass"
    return textwrap.dedent(f"""\
        log("persistent_counter", run_counter + 1)
        computer.moveTo((2, 2))
        computer.scroll(-1, x=computer.width // 2, y=computer.height // 2)
        {horizontal}
        computer.hotkey("win", "r")
        time.sleep(1)
        computer.write("Unicode: 你好😀")
        display(computer.screenshot())
        computer.hotkey("ctrl", "a")
        computer.press("backspace")
        computer.press("esc")
        display(computer.screenshot())
        log("exercise_completed")
    """)


async def main() -> None:
    from tempfile import TemporaryDirectory

    from app.guest.windows import run_command, validate_navigation, wait_for_visual_ready
    from app.lab_server import start_guest_workspace_lab_server

    config = load_backend_config()
    validate_navigation(config.kind)
    output_directory = Path(__file__).parents[2] / "test-artifacts"
    output_directory.mkdir(exist_ok=True)
    marker = f"remote-runtime-{config.kind}-{time.time_ns()}"
    with TemporaryDirectory(prefix="remote-smoke-") as directory:
        Path(directory, "index.html").write_text("verified", encoding="utf-8")
        server = await start_guest_workspace_lab_server(
            workspace_path=directory, remote_host=config.host, remote_port=config.port,
        )
        runtime = None
        try:
            runtime = await launch_remote_python_runtime(config, execution_timeout_ms=90_000)
            await wait_for_visual_ready(runtime)
            first = await runtime.execute(textwrap.dedent("""\
                import os, time
                run_counter = 41
                log("password_env_visible", any(k.endswith("_PASSWORD") and k.startswith("REMOTE_COMPUTER_") for k in os.environ))
                display(computer.screenshot())
                log("setup_completed")
            """))
            text = validate_result(first, "setup_completed", 1)
            if "password_env_visible False" not in text:
                raise RuntimeError("worker credentials were not removed")
            second = await runtime.execute(exercise_code(config.kind))
            text += validate_result(second, "exercise_completed", 2)
            if "persistent_counter 42" not in text:
                raise RuntimeError("persistent globals were not retained")
            # The callback only executes after the guest writes and reads back
            # the exact marker. A screenshot or early log cannot satisfy it.
            guest_path = rf"C:\Users\Public\remote-runtime-{config.kind}.txt"
            script = (
                "$ErrorActionPreference='Stop'; "
                f"Set-Content -LiteralPath '{guest_path}' -Value '{marker}' -Encoding UTF8; "
                f"if ((Get-Content -LiteralPath '{guest_path}' -Raw).Trim() -cne '{marker}') {{ throw 'marker mismatch' }}; "
                f"Invoke-WebRequest -UseBasicParsing -Uri '{server.url_for()}' | Out-Null"
            )
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            # The Run dialog has a short command field. Start a console first
            # so the encoded verification command cannot be silently truncated.
            await run_command(runtime, "cmd.exe /k")
            command = f"powershell.exe -NoProfile -EncodedCommand {encoded}"
            sent = await runtime.execute(
                f"computer.write({command!r}); computer.press('enter'); log('marker_command_sent')"
            )
            text += validate_result(sent, "marker_command_sent", 0)
            try:
                await server.wait_for_index(timeout=30)
            except TimeoutError as error:
                raise RuntimeError("guest marker read-back callback was not received") from error
            final = await runtime.execute('display(computer.screenshot()); log("verification_completed")')
            text += validate_result(final, "verification_completed", 1)
            paths = save_images(first, output_directory, f"{config.kind}-runtime-first")
            paths += save_images(second, output_directory, f"{config.kind}-runtime-second")
            paths += save_images(final, output_directory, f"{config.kind}-runtime-verified")
            print(json.dumps({"backend": config.kind, "target": runtime.target,
                              "dimensions": [runtime.width, runtime.height], "marker": marker,
                              "guest_marker_verified": True, "text": text, "screenshots": paths},
                             ensure_ascii=False), flush=True)
        except Exception:
            if runtime is not None and runtime.connected:
                diagnostic = await runtime.execute("display(computer.screenshot())")
                save_images(diagnostic, output_directory, f"{config.kind}-runtime-failed")
            raise
        finally:
            try:
                if runtime is not None:
                    await runtime.close()
            finally:
                await server.close()


if __name__ == "__main__":
    asyncio.run(main())
