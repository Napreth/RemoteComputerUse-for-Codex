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
from typing import Any, TextIO

from PIL import Image

# Running this file directly makes its directory sys.path[0]. Add python-app so
# the worker imports the same app package as its parent process.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.input_tracking import set_observer
from app.processes import MAX_PROCESS_OUTPUT_BYTES
from app.remote.computer import RemoteComputer

_emit_lock = threading.Lock()
_protocol_output: TextIO | None = None
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class WorkerOutputTooLarge(ValueError):
    """A response cannot fit in the parent's bounded protocol reader."""


def isolate_protocol_output() -> None:
    """Reserve a private descriptor; ordinary stdout goes to diagnostics forever."""
    global _protocol_output
    if _protocol_output is not None:
        return
    protocol = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    try:
        os.dup2(2, 1)
    except BaseException:
        protocol.close()
        raise
    _protocol_output = protocol


def emit(payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload) + "\n"
    # json.dumps defaults to ASCII escapes, so characters equal wire bytes.
    if len(encoded) > MAX_PROCESS_OUTPUT_BYTES:
        raise WorkerOutputTooLarge("Python output exceeds 12 MiB.")
    if _protocol_output is None:
        raise RuntimeError("Worker protocol output is unavailable.")
    with _emit_lock:
        _protocol_output.write(encoded)
        _protocol_output.flush()


def image_output(value: Image.Image | bytes) -> dict[str, str]:
    if isinstance(value, Image.Image):
        buffer = io.BytesIO()
        value.save(buffer, format="PNG")
        value = buffer.getvalue()
    if not isinstance(value, bytes) or not value.startswith(b"\x89PNG\r\n\x1a\n"):
        raise TypeError("display expects a Pillow image or PNG bytes.")
    if len(value) > MAX_IMAGE_BYTES:
        raise ValueError("PNG image output exceeds its 8 MiB size limit.")
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
        # Match the upstream budget: encoded PNG bytes, not the larger base64
        # transport envelope. Padding does not represent image bytes.
        encoded = item["image_url"].removeprefix("data:image/png;base64,")
        image_bytes += len(encoded) // 4 * 3 - (len(encoded) - len(encoded.rstrip("=")))
        if image_bytes > MAX_IMAGE_BYTES or len(output) >= 250:
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


def handle_request(request: dict[str, Any], namespace: dict[str, Any]) -> dict[str, Any]:
    operation = request.get("operation")
    if operation == "execute":
        code = request.get("code")
        if not isinstance(code, str) or not code.strip() or len(code.encode("utf-8")) > 64 * 1024:
            raise ValueError("Python code must be nonempty and at most 64 KiB.")
        return execute(code, namespace)
    if operation not in {"screenshot", "actions", "status"}:
        return {
            "error": {
                "code": "unsupported_python_operation",
                "message": f"Unsupported worker operation: {operation!r}.",
                "recoverable": True,
            }
        }

    computer = namespace["computer"]
    try:
        if operation == "screenshot":
            return {"output": [image_output(computer.screenshot())]}
        if operation == "actions":
            actions = request.get("actions")
            if not isinstance(actions, list):
                raise TypeError("actions must be a list")
            return {"output": [image_output(computer.run_actions(actions))]}
        return {"data": computer.status()}
    except Exception as error:
        # Validation and capability errors are request-scoped while the
        # transport remains usable. A transport failure is terminal so the
        # parent can run its bounded close/release path.
        try:
            connected = bool(computer.status().get("connected"))
        except (OSError, RuntimeError, TypeError, ValueError):
            connected = False
        if not connected or not isinstance(error, (ValueError, TypeError, NotImplementedError)):
            raise
        return {
            "error": {
                "code": "remote_operation_failed",
                "message": f"{type(error).__name__}: {error}",
                "recoverable": True,
            }
        }


def serve_requests(namespace: dict[str, Any], stream: Any) -> None:
    owned_threads = set(threading.enumerate())
    background_input_possible = False
    for line in stream:
        if len(line.encode("utf-8")) > 512 * 1024:
            raise ValueError("Python request exceeds its size limit.")
        request = json.loads(line)
        if not isinstance(request, dict):
            raise TypeError("Python request must be an object.")
        result = handle_request(request, namespace)
        # Snapshot before reading health as well: a thread can finish between
        # the health reply and the later enumeration, leaving late input held.
        background_input_possible = background_input_possible or any(
            thread not in owned_threads for thread in threading.enumerate()
        )
        # exec_py reports Python exceptions as text, so transport failures may
        # not produce a protocol error. Always publish last-known health too.
        try:
            status = namespace["computer"].status()
            connected = status.get("connected") is True
            input_held = status.get("inputHeld") is True
        except Exception:  # noqa: BLE001 - any health-read failure means the transport is unusable
            connected = False
            input_held = True
        # New Python threads may send input after this reply, or finish after
        # leaving keys held. Retain uncertainty for the rest of this worker's
        # lifetime so its owner releases input after terminating the process.
        background_input_possible = background_input_possible or any(
            thread not in owned_threads for thread in threading.enumerate()
        )
        health = {"connected": connected, "inputHeld": input_held or background_input_possible}
        try:
            emit({"id": request.get("id"), **result, **health})
        except WorkerOutputTooLarge as error:
            emit({"id": request.get("id"), "error": {
                "code": "python_output_too_large", "message": str(error), "recoverable": True,
            }, **health})


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
    isolate_protocol_output()
    set_observer(lambda protocol, identities: emit({
        "kind": "input_identity", "protocol": protocol, "identities": identities,
    }))
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
