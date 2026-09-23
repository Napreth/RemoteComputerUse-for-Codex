from __future__ import annotations

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
