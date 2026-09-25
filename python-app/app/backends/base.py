"""Protocol-independent Computer API implemented by every backend."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Hashable
from typing import Any


class ComputerBackend(ABC):
    """A connected remote framebuffer plus direct remote input channel."""

    kind: str
    width: int
    height: int

    @property
    @abstractmethod
    def connected(self) -> bool:
        """Return whether the transport is still known to be usable."""
        ...

    @abstractmethod
    async def connect(self, config: Any) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def screenshot_png(self) -> bytes: ...

    @abstractmethod
    async def move(self, x: int, y: int) -> None: ...

    @abstractmethod
    async def mouse_button(self, x: int, y: int, button: str, pressed: bool) -> None: ...

    @abstractmethod
    async def scroll(self, x: int, y: int, delta_x: int, delta_y: int) -> None: ...

    def validate_scroll(self, delta_x: int, delta_y: int) -> None:
        """Reject unsupported wheel directions before moving the pointer."""

    @abstractmethod
    async def type_text(self, text: str) -> None: ...

    def validate_text(self, text: str) -> None:
        """Reject text that cannot be sent before input ownership becomes uncertain."""

    @abstractmethod
    async def key_chord(self, keys: list[str]) -> None: ...

    def validate_key_chord(self, keys: list[str]) -> None:
        """Reject an invalid chord before any key-down is attempted."""

    async def key_event(self, key: str, pressed: bool) -> None:
        """Send one physical key transition for persistent Python key holds."""
        raise NotImplementedError("this backend does not support individual key transitions")

    def key_identity(self, key: str) -> Hashable:
        """Identify equivalent key spellings for held-input bookkeeping."""
        return key

    @abstractmethod
    async def release_inputs(self) -> None:
        """Best-effort release of buttons and modifier keys after interruption."""
        ...

    def validate_point(self, x: int, y: int) -> None:
        if not 0 <= x < self.width or not 0 <= y < self.height:
            raise ValueError(f"point ({x}, {y}) is outside {self.width}x{self.height}")
