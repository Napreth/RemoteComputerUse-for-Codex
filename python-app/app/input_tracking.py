"""In-memory identities reported before remote input is sent."""
from __future__ import annotations

from collections.abc import Callable, Iterable

_observer: Callable[[str, list[int]], None] | None = None
_release_seeds: dict[str, set[int]] = {}


def set_observer(observer: Callable[[str, list[int]], None]) -> None:
    global _observer
    _observer = observer


def validate_identities(protocol: str, values: object) -> list[int]:
    def valid(value: object) -> bool:
        return type(value) is int and (
            0 <= value <= 0xffff or (protocol == "vnc" and 0x01000000 <= value <= 0x0110ffff)
        )
    if (protocol not in {"rdp", "vnc"} or not isinstance(values, list)
            or len(values) > 1_114_112
            or any(not valid(value) for value in values)):
        raise ValueError("invalid pending input identities; release cannot be confirmed")
    return values


def seed_release(protocol: str, values: object) -> None:
    _release_seeds[protocol] = set(validate_identities(protocol, values))


class InputTracker:
    def __init__(self, protocol: str) -> None:
        self.protocol = protocol
        self.identities = set(_release_seeds.get(protocol, ()))

    def record(self, values: Iterable[int]) -> None:
        identities = sorted(set(values))
        self.identities.update(identities)
        # Flush the pipe before sending any input. The parent drains this pipe
        # after process exit and before deciding whether cleanup succeeded.
        if _observer is not None and identities:
            for offset in range(0, len(identities), 1024):
                _observer(self.protocol, identities[offset:offset + 1024])

    def mark_uncertain(self) -> None:
        """Report scancode input without treating it as a Unicode identity."""
        if _observer is not None:
            _observer(self.protocol, [])
