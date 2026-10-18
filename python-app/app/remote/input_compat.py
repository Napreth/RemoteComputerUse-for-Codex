"""PyAutoGUI-style input transactions executed on the remote session loop."""

from __future__ import annotations

import asyncio
import math
import operator
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..computer_session import ComputerSession


def seconds(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("duration and interval must be finite and nonnegative")
    return value


def integer(value: int) -> int:
    return operator.index(value)


def normalize_button(button: str | int) -> str:
    """Convert supported PyAutoGUI button names to physical backend buttons."""
    if isinstance(button, str):
        normalized = button.lower()
        aliases = {"left": "left", "middle": "middle", "right": "right",
                   "primary": "left", "secondary": "right"}
        if normalized in aliases:
            return aliases[normalized]
    elif type(button) is int:
        numbers = {1: "left", 2: "middle", 3: "right"}
        if button in numbers:
            return numbers[button]
    raise ValueError("button must be left, middle, right, primary, secondary, 1, 2, or 3")


async def perform(session: ComputerSession, operation: str, **args: Any) -> None:
    """Resolve pointer defaults and complete the whole action under one lock."""
    async with session._lock:
        backend = session._require_backend()
        start_x, start_y = session._position
        raw_x, raw_y = args.get("x"), args.get("y")
        if isinstance(raw_x, Sequence) and not isinstance(raw_x, (str, bytes)):
            if len(raw_x) != 2 or raw_y is not None:
                raise ValueError("coordinate sequences require exactly two values and no separate y")
            raw_x, raw_y = raw_x
        x = start_x if raw_x is None else int(raw_x)
        y = start_y if raw_y is None else int(raw_y)
        if args.get("relative"):
            x, y = start_x + int(raw_x or 0), start_y + int(raw_y or 0)
            x, y = round(x), round(y)

        def trajectory() -> list[tuple[int, int]]:
            backend.validate_point(x, y)
            duration = args.get("duration", 0.0)
            steps = args.get("steps")
            if steps is None and duration <= 0.1:
                return [(x, y)]
            tween: Callable[[float], float] = args.get("tween", lambda value: value)
            if steps is None:
                steps = max(1, min(1000, math.ceil(duration / 0.02)))
            # Validate interpolation before a drag presses a button.
            points = []
            for index in range(1, steps + 1):
                fraction = float(tween(index / steps))
                if not math.isfinite(fraction):
                    raise ValueError("tween must return a finite value")
                point = (round(start_x + (x - start_x) * fraction), round(start_y + (y - start_y) * fraction))
                backend.validate_point(*point)
                points.append(point)
            points[-1] = (x, y)
            return points

        async def move(points: list[tuple[int, int]]) -> None:
            for point in points:
                if args.get("duration", 0) and (len(points) > 1 or args.get("steps") is not None):
                    await asyncio.sleep(args.get("duration", 0) / len(points))
                await session._move(*point)

        async def button(pressed: bool) -> None:
            selected = args.get("button", "left")
            if selected not in {"left", "right", "middle"}:
                raise ValueError("button must be left, right, or middle")
            if pressed:
                session._held_buttons.add(selected)
            await backend.mouse_button(*session._position, selected, pressed)
            if not pressed:
                session._held_buttons.discard(selected)

        if operation == "move":
            await move(trajectory())
        elif operation == "drag":
            points = trajectory()
            try:
                await button(True)
                await move(points)
            finally:
                await button(False)
        elif operation in {"click", "mouse"}:
            backend.validate_point(x, y)
            await move(trajectory())
            if operation == "mouse":
                await button(args["pressed"])
            else:
                for _ in range(args["clicks"]):
                    try:
                        await button(True)
                    finally:
                        await button(False)
                    await asyncio.sleep(args["interval"])
        elif operation == "scroll":
            backend.validate_point(x, y)
            clicks = args["clicks"]
            horizontal = args.get("horizontal")
            backend.validate_scroll(clicks * 120 if horizontal else 0,
                                    0 if horizontal else -clicks * 120)
            await session._move(x, y)
            remaining = abs(clicks)
            while remaining:
                notches = min(20, remaining)
                delta = notches * 120 * (1 if clicks > 0 else -1)
                await session._scroll(x, y, delta if horizontal else 0,
                                      0 if horizontal else -delta)
                remaining -= notches
        elif operation == "key":
            key, pressed = args["key"], args["pressed"]
            identity = backend.key_identity(key)
            if identity is not None:
                if pressed:
                    session._held_keys.add(identity)
                try:
                    await backend.key_event(key, pressed)
                except BaseException:
                    session._input_uncertain = True
                    raise
                if not pressed:
                    session._held_keys.discard(identity)
        elif operation == "hotkey":
            resolved_keys = [(key, backend.key_identity(key)) for key in args["keys"]]
            pressed_keys = []
            attempted_input = False
            try:
                try:
                    for key, identity in resolved_keys:
                        pressed_keys.append((key, identity))
                        if identity is not None:
                            session._held_keys.add(identity)
                            attempted_input = True
                            await backend.key_event(key, True)
                        await asyncio.sleep(args["interval"])
                finally:
                    errors = []
                    for key, identity in reversed(pressed_keys):
                        try:
                            if identity is not None:
                                attempted_input = True
                                await backend.key_event(key, False)
                                session._held_keys.discard(identity)
                            await asyncio.sleep(args["interval"])
                        except Exception as error:  # noqa: BLE001 - attempt every release
                            errors.append(error)
                    if errors:
                        raise errors[0]
            except BaseException:
                if attempted_input:
                    session._input_uncertain = True
                raise
        elif operation == "press":
            for _ in range(args["presses"]):
                for key in args["keys"]:
                    await session._keypress([key])
                await asyncio.sleep(args["interval"])
        elif operation == "write_keys":
            for key in args["keys"]:
                await session._keypress([key.lower() if len(key) > 1 else key])
                await asyncio.sleep(args["interval"])
        elif operation == "write":
            text = args["text"]
            interval = args["interval"]
            if not interval:
                await session._type_text(text)
            else:
                for character in text:
                    await session._type_text(character)
                    await asyncio.sleep(interval)
        else:
            raise ValueError(f"unsupported compatibility action: {operation}")
