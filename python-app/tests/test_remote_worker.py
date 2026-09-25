from __future__ import annotations

import io
import random

from PIL import Image

from app.remote.worker import execute


class FakeComputer:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def screenshot(self) -> Image.Image:
        self.events.append(("screenshot",))
        return Image.new("RGB", (4, 3), "navy")

    def click(self, x: int, y: int) -> None:
        self.events.append(("click", x, y))


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


def test_display_uses_upstream_png_byte_budget():
    image = Image.frombytes("RGB", (1600, 1400), random.Random(0).randbytes(1600 * 1400 * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    assert 6 * 1024 * 1024 < len(buffer.getvalue()) < 8 * 1024 * 1024
    assert execute("display(image)", {"image": image})["output"][0]["type"] == "input_image"
    result = execute("display(image); display(image)", {"image": image})
    assert result["output"][0]["type"] == "input_image"
    assert "Python image output exceeds its size limit" in result["output"][-1]["text"]
