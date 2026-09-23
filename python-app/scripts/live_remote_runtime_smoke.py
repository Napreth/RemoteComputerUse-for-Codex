"""Opt-in live smoke test for the persistent exec_py-style remote runtime."""

from __future__ import annotations

import asyncio
import base64
import json
import os
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


async def main() -> None:
    config = load_backend_config()
    output_directory = Path(__file__).parents[2] / "test-artifacts"
    output_directory.mkdir(exist_ok=True)
    runtime = await launch_remote_python_runtime(config, execution_timeout_ms=90_000)
    prompt_ready = os.getenv("REMOTE_COMPUTER_SMOKE_PROMPT_READY") == "1"
    try:
        first = await runtime.execute(
            textwrap.dedent(
                """
                import os, time
                run_counter = 41
                deadline = time.monotonic() + 30
                while True:
                    frame = computer.screenshot()
                    histogram = frame.convert("L").histogram()
                    if sum(histogram[16:]) / max(1, sum(histogram)) >= 0.05:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("remote framebuffer did not become visually ready")
                    time.sleep(0.5)
                log("ready", computer.backend, computer.size())
                log("password_env_visible", any(k.endswith("_PASSWORD") and k.startswith("REMOTE_COMPUTER_") for k in os.environ))
                display(frame)
                """
            )
        )
        marker = f"remote-runtime-{config.kind}-{time.time_ns()}"
        second = await runtime.execute(
            textwrap.dedent(
                rf"""
                log("persistent_counter", run_counter + 1)
                computer.press("esc")
                time.sleep(0.4)
                computer.move_to(2, 2)
                computer.click()
                computer.scroll(120, x=computer.width // 2, y=computer.height // 2, delta_x={120 if config.kind == 'vnc' else 0})
                if {prompt_ready!r}:
                    computer.click(computer.width // 2, computer.height // 2)
                    time.sleep(1)
                    computer.write("echo {config.kind.upper()} Unicode: 你好")
                elif computer.backend == "vnc":
                    computer.hotkey("ctrl", "esc")
                    time.sleep(1)
                    computer.write("cmd")
                    time.sleep(0.5)
                    computer.press("enter")
                    time.sleep(2)
                    computer.write("echo {config.kind.upper()} Unicode: 你好")
                else:
                    computer.hotkey("win", "r")
                    time.sleep(1)
                    computer.write("{config.kind.upper()} Unicode: 你好")
                time.sleep(0.75)
                display(computer.screenshot())
                if {prompt_ready!r}:
                    computer.press("enter")
                    time.sleep(0.5)
                elif computer.backend == "vnc":
                    computer.press("enter")
                    time.sleep(0.5)
                else:
                    computer.hotkey("ctrl", "a")
                time.sleep(0.25)
                command = r"cmd /c echo {marker}>C:\Users\Public\remote-runtime-{config.kind}.txt"
                computer.write(command)
                time.sleep(0.75)
                computer.press("enter")
                time.sleep(3)
                display(computer.screenshot())
                """
            )
        )
        text = [item["text"] for result in (first, second) for item in result["output"] if item["type"] == "input_text"]
        if "password_env_visible False" not in text or "persistent_counter 42" not in text:
            raise RuntimeError(f"persistent runtime invariants failed: {text}")
        paths = save_images(first, output_directory, f"{config.kind}-runtime-first")
        paths += save_images(second, output_directory, f"{config.kind}-runtime-second")
        print(
            json.dumps(
                {
                    "backend": config.kind,
                    "target": runtime.target,
                    "dimensions": [runtime.width, runtime.height],
                    "marker": marker,
                    "text": text,
                    "screenshots": paths,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
