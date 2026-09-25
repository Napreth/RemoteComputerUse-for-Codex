from __future__ import annotations

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
