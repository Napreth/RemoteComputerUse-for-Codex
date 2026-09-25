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
import time
from collections import namedtuple
from collections.abc import Callable, Coroutine, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, TypeVar

from PIL import Image

from ..actions import Point, parse_computer_action
from ..backends import BackendConfig, load_backend_config
from ..computer_session import ComputerSession
from .input_compat import integer, perform, seconds

T = TypeVar("T")
Size = namedtuple("Size", "width height")
Position = namedtuple("Position", "x y")


class RemoteComputer:
    """Blocking, code-tool-friendly access to one remote desktop connection."""

    PAUSE = 0.1

    @staticmethod
    def linear(value: float) -> float:
        return value

    def _input(self, operation: str, **args: Any) -> None:
        self._call(perform(self._session, operation, **args))
        time.sleep(seconds(self.PAUSE))

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
            return {**self._session.status(),
                    "inputHeld": bool(self._session._held_keys or self._session._held_buttons)}

        return self._call(read())

    def size(self) -> tuple[int, int]:
        return Size(self.width, self.height)

    def screenshot(
        self, imageFilename: str | None = None, region: tuple[int, int, int, int] | None = None,
    ) -> Image.Image:
        """Return a Pillow image in the same pixel coordinate space as input."""
        image = Image.open(io.BytesIO(self._call(self._session.screenshot()))).copy()
        if region is not None:
            x, y, width, height = region
            image = image.crop((x, y, x + width, y + height))
        if imageFilename is not None:
            image.save(imageFilename)
        return image

    def move_to(self, x: int | None = None, y: int | None = None, duration: float = 0,
                tween: Callable[[float], float] = linear) -> None:
        self._input("move", x=x, y=y, duration=seconds(duration), tween=tween)

    moveTo = move_to

    def position(self, x: int | None = None, y: int | None = None) -> tuple[int, int]:
        current = self._call(self._session.position())
        return Position(current[0] if x is None else x, current[1] if y is None else y)

    def move(self, xOffset: int | None = None, yOffset: int | None = None, duration: float = 0,
             tween: Callable[[float], float] = linear) -> None:
        self._input("move", x=xOffset, y=yOffset, relative=True, duration=seconds(duration), tween=tween)

    moveRel = move

    def onScreen(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def click(
        self,
        x: int | None = None,
        y: int | None = None,
        clicks: int = 1,
        interval: float = 0.0,
        button: str = "left",
    ) -> None:
        self._input("click", x=x, y=y, button=button, clicks=max(0, integer(clicks)), interval=seconds(interval))

    def doubleClick(self, x: int | None = None, y: int | None = None, interval: float = 0,
                    button: str = "left") -> None:
        self.click(x, y, 2, interval, button)

    def tripleClick(self, x: int | None = None, y: int | None = None, interval: float = 0,
                    button: str = "left") -> None:
        self.click(x, y, 3, interval, button)

    def rightClick(self, x: int | None = None, y: int | None = None) -> None:
        self.click(x, y, button="right")

    def mouseDown(self, x: int | None = None, y: int | None = None, button: str = "left") -> None:
        self._input("mouse", x=x, y=y, button=button, pressed=True)

    def mouseUp(self, x: int | None = None, y: int | None = None, button: str = "left") -> None:
        self._input("mouse", x=x, y=y, button=button, pressed=False)

    def drag_path(
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

    def drag_to(
        self,
        x: int | None = None,
        y: int | None = None,
        duration: float = 0,
        tween: Callable[[float], float] = linear,
        button: str = "left",
        steps: int = 12,
    ) -> None:
        if not 1 <= steps <= 99:
            raise ValueError("steps must be between 1 and 99")
        self._input("drag", x=x, y=y, duration=seconds(duration), tween=tween, button=button)

    dragTo = drag_to

    def drag(self, xOffset: Any, yOffset: int | None = None, duration: float = 0,
             tween: Callable[[float], float] = linear, button: str = "left") -> None:
        if isinstance(xOffset, (list, tuple)) and yOffset is None:
            self.drag_path(xOffset, duration=duration, button=button)
        else:
            self._input("drag", x=xOffset, y=yOffset, relative=True, duration=seconds(duration),
                        tween=tween, button=button)

    dragRel = drag

    def scroll(self, clicks: int, x: int | None = None, y: int | None = None) -> None:
        self._input("scroll", x=x, y=y, clicks=integer(clicks))

    vscroll = scroll

    def hscroll(self, clicks: int, x: int | None = None, y: int | None = None) -> None:
        self._input("scroll", x=x, y=y, clicks=integer(clicks), horizontal=True)

    def write(self, text: str, interval: float = 0) -> None:
        self._input("write", text=text, interval=seconds(interval))

    type_text = write
    typewrite = write

    def keypress(self, *keys: str | Sequence[str], interval: float = 0) -> None:
        selected: list[str]
        if len(keys) == 1 and not isinstance(keys[0], str):
            selected = list(keys[0])
        else:
            selected = [str(key) for key in keys]
        self._input("hotkey", keys=selected, interval=seconds(interval))

    hotkey = keypress

    def press(self, keys: str | Sequence[str], presses: int = 1, interval: float = 0) -> None:
        self._input("press", keys=[keys] if isinstance(keys, str) else list(keys),
                    presses=max(0, integer(presses)), interval=seconds(interval))

    def keyDown(self, key: str) -> None:
        self._input("key", key=key.lower(), pressed=True)

    def keyUp(self, key: str) -> None:
        self._input("key", key=key.lower(), pressed=False)

    @contextmanager
    def hold(self, keys: str | Sequence[str]) -> Iterator[None]:
        pressed: list[str] = []
        try:
            for key in [keys] if isinstance(keys, str) else keys:
                pressed.append(key)
                self.keyDown(key)
            yield
        finally:
            errors = []
            for key in reversed(pressed):
                try:
                    self.keyUp(key)
                except Exception as error:  # noqa: BLE001 - release every held key
                    errors.append(error)
            if errors:
                raise errors[0]

    def run_actions(self, actions: Sequence[dict[str, Any]]) -> Image.Image:
        """Run a validated action batch and return its final screenshot."""
        parsed = [parse_computer_action(action) for action in actions]
        png = self._call(self._session.run_actions(parsed))
        return Image.open(io.BytesIO(png)).copy()
