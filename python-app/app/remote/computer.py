"""Synchronous facade over the asynchronous remote ComputerSession.

Model-authored Python runs on the worker's main thread. The selected RDP or
VNC backend lives on one private asyncio thread, so a single exec_py call can
perform many actions, inspect screenshots, and loop without returning to the
model between each operation.
"""

from __future__ import annotations

import asyncio
import io
import threading
from collections.abc import Coroutine, Sequence
from typing import Any, TypeVar

from PIL import Image

from ..actions import Point, parse_computer_action
from ..backends import BackendConfig, load_backend_config
from ..computer_session import ComputerSession

T = TypeVar("T")


class RemoteComputer:
    """Blocking, code-tool-friendly access to one remote desktop connection."""

    def __init__(
        self,
        config: BackendConfig | None = None,
        *,
        session: ComputerSession | None = None,
    ) -> None:
        self._config = config or load_backend_config()
        self._session = session or ComputerSession()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve_loop, name="remote-computer-io", daemon=True)
        self._closed = False
        self._x = 0
        self._y = 0
        self._thread.start()
        try:
            status = self._call(self._session.connect(self._config))
        except BaseException:
            self._stop_loop()
            raise
        self.width = int(status["width"])
        self.height = int(status["height"])
        self.backend = str(status["backend"])
        self.target = f"{self.backend}://{status['host']}:{status['port']}"

    def _serve_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    def _call(self, operation: Coroutine[Any, Any, T]) -> T:
        if self._closed:
            operation.close()
            raise RuntimeError("remote computer is closed")
        return asyncio.run_coroutine_threadsafe(operation, self._loop).result()

    def _stop_loop(self) -> None:
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._call(self._session.disconnect())
        finally:
            self._closed = True
            self._stop_loop()

    def status(self) -> dict[str, Any]:
        async def read() -> dict[str, Any]:
            return self._session.status()

        return self._call(read())

    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def screenshot(self, region: tuple[int, int, int, int] | None = None) -> Image.Image:
        """Return a Pillow image in the same pixel coordinate space as input."""
        image = Image.open(io.BytesIO(self._call(self._session.screenshot()))).copy()
        if region is not None:
            x, y, width, height = region
            image = image.crop((x, y, x + width, y + height))
        return image

    def move_to(self, x: int, y: int) -> None:
        self._call(self._session.move(x, y))
        self._x, self._y = x, y

    moveTo = move_to

    def click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        button: str = "left",
        clicks: int = 1,
    ) -> None:
        x = self._x if x is None else x
        y = self._y if y is None else y
        self._call(self._session.click(x, y, button, clicks))
        self._x, self._y = x, y

    def drag(
        self,
        path: Sequence[tuple[int, int] | dict[str, int] | Point],
        *,
        button: str = "left",
        duration: float = 0.3,
    ) -> None:
        points = [
            item
            if isinstance(item, Point)
            else Point.model_validate({"x": item[0], "y": item[1]} if isinstance(item, tuple) else item)
            for item in path
        ]
        self._call(self._session.drag(points, button, round(duration * 1000)))
        self._x, self._y = points[-1].x, points[-1].y

    def drag_to(
        self,
        x: int,
        y: int,
        *,
        duration: float = 0.3,
        button: str = "left",
        steps: int = 12,
    ) -> None:
        if not 1 <= steps <= 99:
            raise ValueError("steps must be between 1 and 99")
        start_x, start_y = self._x, self._y
        path = [
            Point(
                x=round(start_x + (x - start_x) * index / steps),
                y=round(start_y + (y - start_y) * index / steps),
            )
            for index in range(steps + 1)
        ]
        self._call(self._session.drag(path, button, round(duration * 1000)))
        self._x, self._y = x, y

    dragTo = drag_to

    def scroll(self, delta_y: int, *, x: int | None = None, y: int | None = None, delta_x: int = 0) -> None:
        x = self._x if x is None else x
        y = self._y if y is None else y
        self._call(self._session.scroll(x, y, delta_x, delta_y))
        self._x, self._y = x, y

    def write(self, text: str) -> None:
        self._call(self._session.type_text(text))

    type_text = write

    def keypress(self, *keys: str | Sequence[str]) -> None:
        selected: list[str]
        if len(keys) == 1 and not isinstance(keys[0], str):
            selected = list(keys[0])
        else:
            selected = [str(key) for key in keys]
        self._call(self._session.keypress(selected))

    hotkey = keypress

    def press(self, key: str) -> None:
        self.keypress(key)

    def run_actions(self, actions: Sequence[dict[str, Any]]) -> Image.Image:
        """Run a validated action batch and return its final screenshot."""
        parsed = [parse_computer_action(action) for action in actions]
        png = self._call(self._session.run_actions(parsed))
        return Image.open(io.BytesIO(png)).copy()
