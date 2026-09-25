"""Opt-in check of guest lab delivery and subsequent framebuffer capture.

The screenshot is available for manual inspection; its rendered content is not
asserted. An external URL also skips the lab server's delivery acknowledgement.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from PIL import Image

from app.backends import load_backend_config
from app.lab_server import start_guest_workspace_lab_server
from app.remote_runtime import launch_remote_session


async def main() -> None:
    root = Path(__file__).parents[2]
    workspace = root / "labs" / "kanban-lab-template"
    output = root / "test-artifacts" / "guest-kanban-framebuffer.png"
    config = load_backend_config()
    external_url = os.getenv("REMOTE_COMPUTER_LAB_SMOKE_URL")
    server = None
    if external_url is None:
        server = await start_guest_workspace_lab_server(
            workspace_path=workspace,
            remote_host=config.host,
            remote_port=config.port,
        )
    session = None
    try:
        session = await launch_remote_session(config, output.parent)
        if external_url is None:
            assert server is not None
            url = server.url_for()
        else:
            url = external_url
        await session.navigate(url)
        if server is not None:
            await server.wait_for_index(timeout=30.0)
        # Capture after the request and allow the browser to paint its assets.
        # navigate()'s earlier frame can still show a blank loading tab.
        await asyncio.sleep(2.0)
        frame = await session.execution.capture_screenshot()
        output.parent.mkdir(exist_ok=True)
        output.write_bytes(frame)
        with Image.open(output) as image:
            dimensions = image.size
        print(
            json.dumps(
                {
                    "backend": config.kind,
                    "guest_url": url,
                    "guest_requested_index": server is not None,
                    "dimensions": dimensions,
                    "screenshot": str(output),
                }
            ),
            flush=True,
        )
    finally:
        if session is not None:
            await session.close()
        if server is not None:
            await server.close()


if __name__ == "__main__":
    asyncio.run(main())
