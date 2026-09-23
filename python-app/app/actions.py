"""Strongly typed, schema-producing remote Computer actions."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

type MouseButton = Literal["left", "right", "middle"]
type ClickCount = Literal[1, 2]


class _ActionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Point(_ActionModel):
    x: int
    y: int


class MoveAction(_ActionModel):
    type: Literal["move"]
    x: int
    y: int


class ClickAction(_ActionModel):
    type: Literal["click"]
    x: int
    y: int
    button: MouseButton = "left"
    count: ClickCount = 1


class DragAction(_ActionModel):
    type: Literal["drag"]
    path: Annotated[list[Point], Field(min_length=2, max_length=100)]
    button: MouseButton = "left"
    duration_ms: Annotated[int, Field(ge=0, le=10_000)] = 300


class ScrollAction(_ActionModel):
    type: Literal["scroll"]
    x: int
    y: int
    delta_x: int = 0
    delta_y: int = 0


class TypeAction(_ActionModel):
    type: Literal["type"]
    text: Annotated[str, Field(max_length=10_000)]


class KeypressAction(_ActionModel):
    type: Literal["keypress"]
    keys: Annotated[list[str], Field(min_length=1, max_length=8)]


class WaitAction(_ActionModel):
    type: Literal["wait"]
    seconds: Annotated[float, Field(ge=0, le=5)] = 0.5


type ComputerAction = Annotated[
    MoveAction | ClickAction | DragAction | ScrollAction | TypeAction | KeypressAction | WaitAction,
    Field(discriminator="type"),
]

_ACTION_ADAPTER: TypeAdapter[ComputerAction] = TypeAdapter(ComputerAction)


def parse_computer_action(value: object) -> ComputerAction:
    """Validate an untrusted action against the same union exposed by MCP."""
    return _ACTION_ADAPTER.validate_python(value)
