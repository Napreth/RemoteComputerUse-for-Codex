from __future__ import annotations

import io
import json
import random

import pytest
from PIL import Image

from app.remote.worker import execute, handle_request, serve_requests


class FakeComputer:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def screenshot(self) -> Image.Image:
        self.events.append(("screenshot",))
        return Image.new("RGB", (4, 3), "navy")

    def click(self, x: int, y: int) -> None:
        self.events.append(("click", x, y))

    def run_actions(self, actions: list[dict]) -> Image.Image:
        raise ValueError("point is outside framebuffer")

    def status(self) -> dict[str, bool]:
        return {"connected": True}


def test_display_uses_upstream_png_byte_budget():
    image = Image.frombytes("RGB", (1600, 1400), random.Random(0).randbytes(1600 * 1400 * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    assert 6 * 1024 * 1024 < len(buffer.getvalue()) < 8 * 1024 * 1024
    assert execute("display(image)", {"image": image})["output"][0]["type"] == "input_image"
    result = execute("display(image); display(image)", {"image": image})
    assert result["output"][0]["type"] == "input_image"
    assert "Python image output exceeds its size limit" in result["output"][-1]["text"]


def test_exec_namespace_persists_and_one_call_can_loop_over_remote_actions() -> None:
    computer = FakeComputer()
    namespace = {"computer": computer, "Image": Image}

    execute("counter = 40", namespace)
    result = execute(
        "for x in range(3):\n"
        "    computer.click(x, x + 1)\n"
        "counter += 2\n"
        "display(computer.screenshot())\n"
        "log(counter)",
        namespace,
    )

    assert computer.events == [
        ("click", 0, 1),
        ("click", 1, 2),
        ("click", 2, 3),
        ("screenshot",),
    ]
    assert result["output"][0]["type"] == "input_image"
    assert result["output"][1] == {"type": "input_text", "text": "42"}


def test_atomic_operation_error_is_structured_and_namespace_survives() -> None:
    computer = FakeComputer()
    namespace = {"computer": computer, "Image": Image}
    handle_request({"operation": "execute", "code": "counter = 41"}, namespace)

    failure = handle_request({"operation": "actions", "actions": [{"type": "click"}]}, namespace)
    result = handle_request({"operation": "execute", "code": "log(counter + 1)"}, namespace)

    assert failure == {
        "error": {
            "code": "remote_operation_failed",
            "message": "ValueError: point is outside framebuffer",
            "recoverable": True,
        }
    }
    assert result["output"][0] == {"type": "input_text", "text": "42"}


@pytest.mark.parametrize("closed", [False, True])
def test_exec_reports_transport_health_even_when_python_error_is_caught(monkeypatch, closed):
    class DisconnectedComputer(FakeComputer):
        def screenshot(self):
            raise RuntimeError("transport lost")

        def status(self):
            if closed:
                raise RuntimeError("remote computer is closed")
            return {"connected": False}

    results = []
    monkeypatch.setattr("app.remote.worker.emit", results.append)
    request = {"id": 1, "operation": "execute", "code": "computer.screenshot()"}
    serve_requests({"computer": DisconnectedComputer()}, io.StringIO(json.dumps(request) + "\n"))
    assert results[0]["connected"] is False
    assert "transport lost" in results[0]["output"][0]["text"]


def test_background_input_risk_survives_tool_reply_and_thread_exit(monkeypatch):
    import threading

    gate = threading.Event()
    replies = []
    namespace = {"computer": FakeComputer(), "threading": threading, "gate": gate}
    requests = [
        {"id": 1, "operation": "execute", "code":
         "thread = threading.Thread(target=gate.wait)\nthread.start()"},
        {"id": 2, "operation": "execute", "code": "gate.set(); thread.join()"},
    ]
    monkeypatch.setattr("app.remote.worker.emit", replies.append)
    try:
        serve_requests(namespace, io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n"))
        assert [reply["inputHeld"] for reply in replies] == [True, True]
    finally:
        gate.set()
        if "thread" in namespace:
            namespace["thread"].join(timeout=2)


def test_background_thread_finishing_during_health_read_is_still_tracked(monkeypatch):
    import threading

    gate = threading.Event()
    namespace = {"threading": threading, "gate": gate}
    class FinishingComputer(FakeComputer):
        def status(self):
            # Emulate a background action arriving after the health snapshot.
            gate.set()
            namespace["thread"].join(timeout=2)
            return {"connected": True, "inputHeld": False}
    namespace["computer"] = FinishingComputer()
    replies = []
    monkeypatch.setattr("app.remote.worker.emit", replies.append)
    request = {"id": 1, "operation": "execute", "code":
               "thread = threading.Thread(target=gate.wait)\nthread.start()"}
    try:
        serve_requests(namespace, io.StringIO(json.dumps(request) + "\n"))
        assert replies[0]["inputHeld"] is True
    finally:
        gate.set()
        if "thread" in namespace:
            namespace["thread"].join(timeout=2)


def test_identity_and_response_frames_do_not_interleave(monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor

    from app.remote import worker

    chunks = []
    class SlowOutput:
        def write(self, value):
            middle = len(value) // 2
            chunks.append(value[:middle])
            time.sleep(0.001)
            chunks.append(value[middle:])
        def flush(self):
            pass
    monkeypatch.setattr(worker, "_protocol_output", SlowOutput())
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda value: worker.emit({"id": value}), range(20)))
    assert {json.loads(line)["id"] for line in "".join(chunks).splitlines()} == set(range(20))


def test_oversized_response_is_replaced_before_emitting_any_protocol_bytes(monkeypatch):
    from app.remote import worker

    class LargeStatusComputer(FakeComputer):
        def status(self):
            return {"connected": True, "inputHeld": True, "padding": "x" * 1000}

    stream = io.StringIO()
    monkeypatch.setattr(worker, "_protocol_output", stream)
    monkeypatch.setattr(worker, "MAX_PROCESS_OUTPUT_BYTES", 512)
    requests = [{"id": 1, "operation": "status"}, {"id": 2, "operation": "execute", "code": "log(42)"}]
    serve_requests({"computer": LargeStatusComputer()}, io.StringIO("\n".join(map(json.dumps, requests)) + "\n"))
    lines = stream.getvalue().splitlines(keepends=True)
    assert len(lines) == 2 and all(len(line.encode()) <= 512 for line in lines)
    first, second = map(json.loads, lines)
    assert first["id"] == 1
    assert first["error"]["code"] == "python_output_too_large"
    assert first["error"]["recoverable"] is True
    assert first["connected"] is True and first["inputHeld"] is True
    assert second["id"] == 2 and second["output"] == [{"type": "input_text", "text": "42"}]
