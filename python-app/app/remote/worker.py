"""Persistent Python REPL connected to one RDP or VNC computer."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from types import ModuleType
from typing import Any

from PIL import Image

# Running this file directly makes its directory sys.path[0]. Add python-app so
# the worker imports the same app package as its parent process.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.remote.computer import RemoteComputer


def emit(payload: dict[str, Any]) -> None:
    if sys.__stdout__ is None:
        raise RuntimeError("Worker stdout is unavailable.")
    sys.__stdout__.write(json.dumps(payload) + "\n")
    sys.__stdout__.flush()


def image_output(value: Image.Image | bytes) -> dict[str, str]:
    if isinstance(value, Image.Image):
        buffer = io.BytesIO()
        value.save(buffer, format="PNG")
        value = buffer.getvalue()
    if not isinstance(value, bytes) or not value.startswith(b"\x89PNG\r\n\x1a\n"):
        raise TypeError("display expects a Pillow image or PNG bytes.")
    return {
        "type": "input_image",
        "detail": "original",
        "image_url": "data:image/png;base64," + base64.b64encode(value).decode("ascii"),
    }


def execute(code: str, namespace: dict[str, Any]) -> dict[str, Any]:
    output: list[dict[str, Any]] = []
    text_bytes = 0
    image_bytes = 0

    def log(*values: Any) -> None:
        nonlocal text_bytes
        text = " ".join(str(value) for value in values)
        text_bytes += len(text.encode("utf-8"))
        if text_bytes > 60 * 1024 or len(output) >= 250:
            raise ValueError("Python text output exceeds its size limit.")
        output.append({"type": "input_text", "text": text})

    def display(value: Image.Image | bytes) -> None:
        nonlocal image_bytes
        item = image_output(value)
        encoded = item["image_url"].removeprefix("data:image/png;base64,")
        image_bytes += len(encoded) // 4 * 3 - (len(encoded) - len(encoded.rstrip("=")))
        if image_bytes > 8 * 1024 * 1024 or len(output) >= 250:
            raise ValueError("Python image output exceeds its size limit.")
        output.append(item)

    class Writer:
        def write(self, text: str) -> int:
            if text.strip():
                log(text)
            return len(text)

        def flush(self) -> None:
            pass

    namespace.update(log=log, display=display)
    try:
        with contextlib.redirect_stdout(Writer()), contextlib.redirect_stderr(Writer()):
            exec(compile(code, "<exec_py>", "exec"), namespace)  # noqa: S102 - isolated model-code worker
    except BaseException:  # noqa: BLE001 - report model errors while retaining the REPL
        output.append({"type": "input_text", "text": traceback.format_exc()[-4000:]})
    return {"output": output or [{"type": "input_text", "text": "exec_py completed with no output."}]}


def serve_requests(namespace: dict[str, Any], stream: Any) -> None:
    for line in stream:
        if len(line.encode("utf-8")) > 512 * 1024:
            raise ValueError("Python request exceeds its size limit.")
        request = json.loads(line)
        if not isinstance(request, dict):
            raise TypeError("Python request must be an object.")
        operation = request.get("operation")
        if operation == "execute":
            code = request.get("code")
            if not isinstance(code, str) or not code.strip() or len(code.encode("utf-8")) > 64 * 1024:
                raise ValueError("Python code must be nonempty and at most 64 KiB.")
            result = execute(code, namespace)
        elif operation == "screenshot":
            result = {"output": [image_output(namespace["computer"].screenshot())]}
        elif operation == "actions":
            actions = request.get("actions")
            if not isinstance(actions, list):
                raise TypeError("actions must be a list")
            result = {"output": [image_output(namespace["computer"].run_actions(actions))]}
        elif operation == "status":
            result = {"data": namespace["computer"].status()}
        else:
            result = {
                "error": {
                    "code": "unsupported_python_operation",
                    "message": f"Unsupported worker operation: {operation!r}.",
                }
            }
        input_held = namespace["computer"].status().get("inputHeld") is True
        emit({"id": request.get("id"), **result, "inputHeld": input_held})


class RemotePyAutoGUI(ModuleType):
    """Route imports to the remote facade without importing host input APIs."""

    def __init__(self, computer: RemoteComputer) -> None:
        super().__init__("pyautogui")
        self.__dict__["_computer"] = computer

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_computer"], name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "PAUSE":
            setattr(self.__dict__["_computer"], name, value)
        else:
            super().__setattr__(name, value)



def main() -> None:
    computer = RemoteComputer()
    try:
        # Model-authored code can import os, so remove connection secrets after
        # the backend has consumed them. The parent retains a private copy for
        # the bounded emergency-release helper.
        os.environ.pop("REMOTE_COMPUTER_RDP_PASSWORD", None)
        os.environ.pop("REMOTE_COMPUTER_VNC_PASSWORD", None)
        pyautogui = RemotePyAutoGUI(computer)
        sys.modules["pyautogui"] = pyautogui
        namespace = {"__builtins__": __builtins__, "computer": computer, "pyautogui": pyautogui, "Image": Image}
        emit(
            {
                "ready": True,
                "platform": "remote",
                "backend": computer.backend,
                "target": computer.target,
                "width": computer.width,
                "height": computer.height,
            }
        )
        if "--check" in sys.argv:
            return
        if os.name != "nt":
            parent = os.getppid()

            def watch_parent() -> None:
                while True:
                    time.sleep(1)
                    if os.getppid() != parent:
                        os.killpg(os.getpgrp(), signal.SIGKILL)  # type: ignore[attr-defined]

            threading.Thread(target=watch_parent, daemon=True).start()
        serve_requests(namespace, sys.stdin)
    finally:
        computer.close()


if __name__ == "__main__":
    try:
        main()
    except BaseException as error:
        emit({"error": f"{type(error).__name__}: {error}"[:4000]})
        raise SystemExit(1) from error
