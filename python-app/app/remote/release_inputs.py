"""Reconnect after an interrupted worker and release common remote inputs."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.backends import load_backend_config
from app.computer_session import ComputerSession


async def release() -> None:
    session = ComputerSession()
    try:
        await session.connect(load_backend_config())
        await session.release_inputs()
    finally:
        await session.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(release())
        print(json.dumps({"released": True}), flush=True)
    except BaseException as error:
        print(json.dumps({"released": False, "error": f"{type(error).__name__}: {error}"[:2000]}), flush=True)
        raise SystemExit(1) from error
