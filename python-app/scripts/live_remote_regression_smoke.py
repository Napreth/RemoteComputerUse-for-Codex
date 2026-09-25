"""Opt-in remote input, session health, and deadline regression checks.

Use only against a disposable guest configured through REMOTE_COMPUTER_*.
This drives the remote pointer and deliberately interrupts its worker.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.actions import DragAction, MoveAction, Point
from app.backends import load_backend_config
from app.plugin_session import PluginSessions
from app.remote.runtime import RemotePythonOperationError, RemotePythonRuntimeError


def require_text(result: dict, expected: str) -> None:
    texts = [item["text"] for item in result["output"] if item["type"] == "input_text"]
    if texts != [expected]:
        raise AssertionError(texts)


async def main() -> None:
    config = load_backend_config()
    sessions = PluginSessions()
    name = "regression"
    artifacts = Path(__file__).resolve().parents[2] / "test-artifacts"
    artifacts.mkdir(exist_ok=True)
    try:
        status = await sessions.connect(config, name)
        print(f"{config.kind}: connected", flush=True)
        old_runtime = sessions._sessions[name].runtime
        assert old_runtime is not None
        _, status = await asyncio.gather(sessions.disconnect(name), sessions.connect(config, name))
        assert old_runtime.child.process.returncode is not None
        assert (await sessions.status(name))["connected"] is True
        require_text(await sessions.execute(
            "from concurrent.futures import ThreadPoolExecutor\n"
            "with ThreadPoolExecutor(max_workers=3) as pool:\n"
            "    pending = [pool.submit(computer.move_to, 20, 30),\n"
            "               pool.submit(computer.click),\n"
            "               pool.submit(computer.drag_to, 40, 40, duration=0.1)]\n"
            "    for future in pending:\n"
            "        future.result(timeout=10)\n"
            "log('threaded input complete')", name,
        ), "threaded input complete")
        print(f"{config.kind}: same-name reconnect and threaded input passed", flush=True)
        width = status["width"]
        await sessions.execute("regression_counter = 41", name)
        await sessions.run_actions([MoveAction(type="move", x=20, y=30)], name)
        require_text(await sessions.execute(
            "assert computer.position() == (20, 30)\n"
            "computer.click()\n"
            "log(regression_counter + 1)", name,
        ), "42")
        try:
            await sessions.run_actions([DragAction(type="drag", path=[
                Point(x=20, y=30), Point(x=width, y=30),
            ])], name)
        except RemotePythonOperationError:
            pass
        else:
            raise AssertionError("Out-of-bounds drag was accepted")
        require_text(await sessions.execute(
            "assert computer.position() == (20, 30)\n"
            "computer.drag_to(40, 40, duration=0.1)\n"
            "log(regression_counter + 1)", name,
        ), "42")
        frame = await sessions.screenshot(name)
        (artifacts / f"{config.kind}-regression.png").write_bytes(frame)
        print(f"{config.kind}: mixed actions and recoverable drag error passed", flush=True)

        # Simulate a transport failure that exec_py catches as ordinary Python.
        require_text(await sessions.execute(
            "computer._call(computer._session.disconnect())\nlog('disconnected')", name,
        ), "disconnected")
        assert (await sessions.list_sessions())[0]["connected"] is False
        print(f"{config.kind}: exec disconnect health and emergency release passed", flush=True)
        await sessions.disconnect(name)

        await sessions.connect(config, name)
        runtime = sessions._sessions[name].runtime
        assert runtime is not None
        runtime.execution_timeout_ms = 1000
        try:
            await sessions.execute("while True: pass", name)
        except RemotePythonRuntimeError as error:
            assert "exceeded 1000ms" in str(error), str(error)
        else:
            raise AssertionError("Infinite code was not interrupted")
        # close() re-raises a release failure; do not accept a timeout alone.
        await runtime.close()
        assert runtime.child.process.returncode is not None
        assert (await sessions.list_sessions())[0]["connected"] is False
        print(json.dumps({"backend": config.kind, "regressions": "passed"}), flush=True)
    finally:
        for entry in await sessions.list_sessions():
            await sessions.disconnect(entry["session"])


if __name__ == "__main__":
    asyncio.run(main())
