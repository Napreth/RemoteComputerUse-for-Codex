from __future__ import annotations

import asyncio
import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts.live_remote_runtime_smoke import exercise_code, validate_result


def test_smoke_rejects_early_invariants_followed_by_traceback():
    result = {"output": [
        {"type": "input_text", "text": "persistent_counter 42"},
        {"type": "input_text", "text": "Traceback (most recent call last): TypeError: invalid scroll"},
    ]}
    with pytest.raises(RuntimeError, match="execution failed"):
        validate_result(result, "exercise_completed", 2)


def test_smoke_requires_terminal_marker_and_final_images():
    with pytest.raises(RuntimeError, match="did not complete"):
        validate_result({"output": [{"type": "input_text", "text": "persistent_counter 42"}]}, "exercise_completed", 2)
    with pytest.raises(RuntimeError, match="did not complete"):
        validate_result({"output": [{"type": "input_text", "text": "exercise_completed"}]}, "exercise_completed", 2)


@pytest.mark.parametrize("backend", ["rdp", "vnc"])
def test_smoke_exercises_current_facade_through_terminal_marker(backend):
    class Computer:
        width, height = 64, 48
        def moveTo(self, point): pass
        def scroll(self, clicks, x=None, y=None): assert clicks == -1
        def hscroll(self, clicks): assert backend == "vnc" and clicks == 1
        def hotkey(self, *keys): pass
        def write(self, text): pass
        def screenshot(self): return b"image"
        def press(self, key): pass
    text, images = [], []
    from types import SimpleNamespace
    exec(exercise_code(backend), {  # noqa: S102 - trusted smoke snippet against an inert fake
        "computer": Computer(), "run_counter": 41,
         "log": lambda *args: text.append(" ".join(map(str, args))), "display": images.append,
         "time": SimpleNamespace(sleep=lambda duration: None)})
    assert text[-1] == "exercise_completed"
    assert len(images) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [None, "runtime", "cleanup", "first-character", "screenshot-error", "screenshot-cancel"],
)
@pytest.mark.parametrize("initial_only", [False, True])
async def test_notepad_smoke_requires_full_execution_and_successful_cleanup(
    tmp_path, monkeypatch, failure, initial_only,
):
    from app.backends.config import RdpConfig
    from scripts import live_rdp_typing_smoke as smoke
    monkeypatch.setattr(time, "sleep", lambda duration: None)

    class Computer:
        text = ""

        def write(self, text, interval=0):
            self.text = text

        def run_actions(self, actions):
            self.text = actions[0]["text"]

        def hotkey(self, *keys):
            pass

    computer = Computer()

    async def execute(code):
        output = []
        exec(code, {  # noqa: S102 - trusted smoke snippet, no real guest or host input
            "computer": computer, "log": lambda value: output.append({"type": "input_text", "text": value}),
        })
        if failure in {"runtime", "screenshot-error", "screenshot-cancel"}:
            output.append({"type": "input_text", "text": "Traceback: failed after early marker"})
        return {"output": output}

    def guest_request(config, vm_name, action, path=""):
        text = computer.text[1:] if failure == "first-character" else computer.text
        value = base64.b64encode(text.encode()).decode() if action == "read" else "test-version"
        return {"result": value, "addresses": ["127.0.0.1"]}

    runtime = SimpleNamespace(
        execute=execute, capture_screenshot=AsyncMock(return_value=b"test-frame"),
        close=AsyncMock(side_effect=RuntimeError("release failed") if failure == "cleanup" else None),
    )
    if failure == "screenshot-error":
        runtime.capture_screenshot.side_effect = OSError("frame unavailable")
    elif failure == "screenshot-cancel":
        runtime.capture_screenshot.side_effect = asyncio.CancelledError()
    monkeypatch.setattr(smoke, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(smoke, "__file__", str(tmp_path / "python-app/scripts/typing.py"))
    monkeypatch.setattr(smoke, "load_profile", lambda name: RdpConfig(host="127.0.0.1", port=3389, username="test"))
    monkeypatch.setattr(smoke, "guest_request", guest_request)
    monkeypatch.setattr(smoke, "run_command", AsyncMock())
    monkeypatch.setattr(smoke, "launch_remote_python_runtime", AsyncMock(return_value=runtime))
    monkeypatch.setattr(smoke, "socket", SimpleNamespace(
        SOCK_STREAM=1, getaddrinfo=lambda *args, **kwargs: [(None, None, None, None, ("127.0.0.1", 3389))],
    ))
    if failure:
        if failure == "screenshot-cancel":
            with pytest.raises(asyncio.CancelledError):
                await smoke.main("disposable-vm", "test-profile", initial_only=initial_only)
        else:
            error_type = AssertionError if failure == "first-character" else RuntimeError
            with pytest.raises(error_type, match="did not complete|release failed|saved guest text differs"):
                await smoke.main("disposable-vm", "test-profile", initial_only=initial_only)
    else:
        await smoke.main("disposable-vm", "test-profile", initial_only=initial_only)
    report = json.loads((tmp_path / "test-artifacts/rdp-notepad-typing.json").read_text())
    assert report["passed"] is (failure is None)
    if failure not in {"runtime", "screenshot-error", "screenshot-cancel"}:
        assert report["cases"][0]["name"] == "initial-unicode"
    if failure == "first-character":
        assert report["cases"][0]["mismatch_at"] == 0
    if failure is None:
        assert len(report["cases"]) == (1 if initial_only else 6)
    if failure == "screenshot-error":
        assert report["screenshot_error"] == "OSError"
    runtime.close.assert_awaited_once()
