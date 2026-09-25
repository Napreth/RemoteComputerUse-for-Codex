"""PyAutoGUI-style input transactions executed on the remote session loop."""

from __future__ import annotations

import asyncio
import math
import operator
from collections.abc import Callable
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


async def perform(session: ComputerSession, operation: str, **args: Any) -> None:
    """Resolve pointer defaults and complete the whole action under one lock."""
    async with session._lock:
        backend = session._require_backend()
        start_x, start_y = session._position
        x = start_x if args.get("x") is None else round(args["x"])
        y = start_y if args.get("y") is None else round(args["y"])
        if args.get("relative"):
            x, y = start_x + (args.get("x") or 0), start_y + (args.get("y") or 0)
            x, y = round(x), round(y)

        def trajectory() -> list[tuple[int, int]]:
            backend.validate_point(x, y)
            duration = args.get("duration", 0.0)
            tween: Callable[[float], float] = args.get("tween", lambda value: value)
            steps = max(1, min(1000, math.ceil(duration / 0.02))) if duration > 0.1 else 1
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
                if len(points) > 1:
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
            await session._move(x, y)
            if operation == "mouse":
                await button(args["pressed"])
            else:
                for index in range(args["clicks"]):
                    if index:
                        await asyncio.sleep(args["interval"])
                    try:
                        await button(True)
                    finally:
                        await button(False)
        elif operation == "scroll":
            backend.validate_point(x, y)
            await session._move(x, y)
            clicks = args["clicks"]
            remaining = abs(clicks)
            while remaining:
                notches = min(20, remaining)
                delta = notches * 120 * (1 if clicks > 0 else -1)
                await session._scroll(x, y, delta if args.get("horizontal") else 0,
                                      0 if args.get("horizontal") else -delta)
                remaining -= notches
        elif operation == "key":
            key, pressed = args["key"], args["pressed"]
            if pressed:
                session._held_keys.add(key)
            await backend.key_event(key, pressed)
            if not pressed:
                session._held_keys.discard(key)
        elif operation == "hotkey":
            pressed_keys = []
            try:
                for key in args["keys"]:
                    pressed_keys.append(key)
                    session._held_keys.add(key)
                    await backend.key_event(key, True)
                    await asyncio.sleep(args["interval"])
            finally:
                errors = []
                for key in reversed(pressed_keys):
                    try:
                        await backend.key_event(key, False)
                        session._held_keys.discard(key)
                        await asyncio.sleep(args["interval"])
                    except Exception as error:  # noqa: BLE001 - attempt every release
                        errors.append(error)
                if errors:
                    raise errors[0]
        elif operation == "press":
            for _ in range(args["presses"]):
                for key in args["keys"]:
                    await backend.key_chord([key])
                await asyncio.sleep(args["interval"])
        elif operation == "write":
            text = args["text"]
            interval = args["interval"]
            if not interval:
                await backend.type_text(text)
            else:
                for character in text:
                    await backend.type_text(character)
                    await asyncio.sleep(interval)
        else:
            raise ValueError(f"unsupported compatibility action: {operation}")
