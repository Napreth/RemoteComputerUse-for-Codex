from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.backends.config import RdpConfig
from app.plugin_session import PluginSessions, SessionEntry
from app.processes import OwnedProcess
from app.remote.runtime import (
    RemotePythonRuntime,
    RemotePythonRuntimeError,
    RemotePythonStartupCleanupError,
    launch_remote_python_runtime,
)

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = RdpConfig(host="fixture", port=3389, username="fixture")


async def runtime(**kwargs):
    return await launch_remote_python_runtime(
        CONFIG,
        python_path=sys.executable,
        worker_path=FIXTURES / "worker.py",
        release_helper_path=kwargs.pop("release_helper_path", FIXTURES / "release_inputs.py"),
        **kwargs,
    )


async def test_persistent_state_and_process_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-worker")
    session = await runtime()
    try:
        await session.execute("counter = 41\nprint(os.getpid())")
        second = await session.execute("print(counter + 1, 'café 😀')")
        assert second["output"][0]["text"] == "42 café 😀\n"
        environment = await session.execute("print([k for k in os.environ if k.upper() == 'OPENAI_API_KEY'])")
        assert environment["output"][0]["text"] == "[]\n"
    finally:
        await session.close()
    assert session.child.process.returncode is not None
    with pytest.raises(RemotePythonRuntimeError, match="closed"):
        await session.execute("print('late')")


async def test_deadline_kills_worker_and_runs_release_helper(tmp_path: Path) -> None:
    helper = tmp_path / "release.py"
    marker = tmp_path / "released"
    session = await runtime(release_helper_path=helper, execution_timeout_ms=100)
    helper.write_text(
        "import json\n"
        f"with open({str(marker)!r}, 'a') as log: log.write('released\\n')\n"
        "print(json.dumps({'released': True}), flush=True)\n"
    )
    with pytest.raises(RemotePythonRuntimeError, match="exceeded 100ms"):
        await session.execute("while True: pass")
    await session.close()
    assert marker.read_text() == "released\n"


async def test_successful_code_does_not_open_cleanup_connection(tmp_path: Path) -> None:
    helper = tmp_path / "must-not-run.py"
    helper.write_text("raise RuntimeError('release helper should not run')")
    session = await runtime(release_helper_path=helper)
    assert (await session.execute("print('ok')"))["output"][0]["text"] == "ok\n"
    await session.close()


@pytest.mark.parametrize("release_in_code", [False, True])
async def test_successful_held_input_survives_status_and_is_released_on_close(tmp_path, release_in_code):
    helper, marker = tmp_path / "release.py", tmp_path / "released"
    helper.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\nprint('{{\"released\":true}}')\n")
    session = await runtime(release_helper_path=helper)
    try:
        await session.execute("input_held = True")
        await session.status()
        if release_in_code:
            await session.execute("input_held = False")
    finally:
        await session.close()
    assert marker.exists() is not release_in_code


async def test_recoverable_action_error_keeps_worker_and_namespace_alive() -> None:
    session = await runtime()
    try:
        await session.execute("counter = 41")
        with pytest.raises(RemotePythonRuntimeError, match="invalid fixture action") as caught:
            await session.run_actions([{"type": "invalid"}])

        assert caught.value.code == "remote_operation_failed"
        assert session.connected is True
        result = await session.execute("print(counter + 1)")
        assert result["output"][0]["text"] == "42\n"
    finally:
        await session.close()


async def test_terminal_worker_error_without_response_id_is_reported() -> None:
    session = await runtime()

    with pytest.raises(RemotePythonRuntimeError, match="fixture transport lost"):
        await session.run_actions([{"type": "fatal"}])

    assert session.connected is False


async def test_status_updates_cached_transport_health_for_session_selection() -> None:
    session = await runtime()
    sessions = PluginSessions()
    sessions._sessions["lost"] = SessionEntry(name="lost", config=CONFIG, runtime=session)
    healthy = SimpleNamespace(connected=True, execute=AsyncMock(return_value={"output": []}))
    sessions._sessions["healthy"] = SessionEntry(name="healthy", config=CONFIG, runtime=healthy)
    try:
        await session.execute("status_connected = False")
        assert (await sessions.status("lost"))["connected"] is False
        assert session.child.process.returncode is None
        assert session.connected is False
        assert (await sessions.list_sessions())[1]["connected"] is False
        await sessions.execute("pass")
        healthy.execute.assert_awaited_once_with("pass")
    finally:
        await session.close()


async def test_exec_transport_loss_closes_worker_and_releases_inputs(tmp_path: Path) -> None:
    helper = tmp_path / "release.py"
    marker = tmp_path / "released"
    helper.write_text(
        "import json\n"
        f"with open({str(marker)!r}, 'a') as log: log.write('released\\n')\n"
        "print(json.dumps({'released': True}), flush=True)\n"
    )
    session = await runtime(release_helper_path=helper)
    try:
        result = await session.execute("connected = False\nraise RuntimeError('transport lost')")
        assert "transport lost" in result["output"][0]["text"]
        assert session.connected is False
        assert session.child.process.returncode is not None
        assert marker.read_text() == "released\n"
    finally:
        await session.close()


async def test_exec_transport_loss_preserves_release_failure(tmp_path: Path) -> None:
    helper = tmp_path / "release.py"
    helper.write_text("import json\nprint(json.dumps({'released': False, 'error': 'release failed'}))\n")
    session = await runtime(release_helper_path=helper)
    with pytest.raises(RemotePythonRuntimeError) as caught:
        await session.execute("connected = False")
    assert caught.value.code == "input_release_failed"
    assert session.connected is False
    with pytest.raises(RemotePythonRuntimeError):
        await session.close()


async def test_transport_loss_overrides_recoverable_action_flag() -> None:
    session = await runtime()
    try:
        with pytest.raises(RemotePythonRuntimeError, match="invalid fixture action"):
            await session.run_actions([{"type": "lost_after_validation"}])
        assert session.connected is False
        assert session.child.process.returncode is not None
    finally:
        await session.close()


async def test_startup_reports_cleanup_failure_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    original_close = RemotePythonRuntime._close
    closed = []

    async def close_then_fail(session):
        # Reap the real fixture worker even though the test simulates failure.
        await original_close(session)
        closed.append(session)
        raise RuntimeError("worker cleanup failed")

    monkeypatch.setattr(RemotePythonRuntime, "_receive", AsyncMock(side_effect=RuntimeError("handshake failed")))
    monkeypatch.setattr(RemotePythonRuntime, "_close", close_then_fail)
    with pytest.raises(RemotePythonStartupCleanupError, match="worker cleanup failed") as caught:
        await runtime()
    assert caught.value.code == "startup_cleanup_failed"
    assert len(closed) == 1
    assert closed[0].child.process.returncode is not None


@pytest.mark.parametrize("startup_fails", [False, True])
async def test_cancelled_plugin_startup_reaps_real_worker_and_allows_unnamed_retry(monkeypatch, startup_fails):
    original_receive = RemotePythonRuntime._receive
    started, finish = asyncio.Event(), asyncio.Event()
    workers = []

    async def receive(session, expected_id, timeout_ms):
        workers.append(session)
        started.set()
        await finish.wait()
        if startup_fails:
            raise RemotePythonRuntimeError("handshake failed")
        return await original_receive(session, expected_id, timeout_ms)

    async def launch(config):
        return await runtime()

    monkeypatch.setattr(RemotePythonRuntime, "_receive", receive)
    monkeypatch.setattr("app.plugin_session.launch_remote_python_runtime", launch)
    sessions = PluginSessions()
    connecting = asyncio.create_task(sessions.connect(CONFIG))
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert workers[0].child.process.returncode is None
        connecting.cancel()
        await asyncio.sleep(0)
        connecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connecting
        cleanup = sessions._sessions["default"].cleanup
        assert cleanup is not None
        finish.set()
        await asyncio.wait_for(asyncio.shield(cleanup), 5)
        assert workers[0].child.process.returncode is not None
        assert await sessions.list_sessions() == []
        monkeypatch.setattr(RemotePythonRuntime, "_receive", original_receive)
        assert (await sessions.connect(CONFIG))["connected"] is True
        result = await sessions.execute("print(42)")
        assert result["output"][0]["text"] == "42\n"
        await sessions.disconnect()
    finally:
        finish.set()
        await asyncio.gather(connecting, return_exceptions=True)
        for worker in workers:
            await worker.close()
        if sessions._sessions:
            await sessions.disconnect("default")


@pytest.mark.parametrize("stage", ["spawn", "handshake"])
@pytest.mark.parametrize("cleanup_result", ["success", "error", "cancelled"])
async def test_startup_task_cancellation_resolves_worker_ownership(monkeypatch, stage, cleanup_result):
    original_spawn = OwnedProcess.spawn
    original_receive = RemotePythonRuntime._receive
    original_close = OwnedProcess._close
    entered, finish_spawn = asyncio.Event(), asyncio.Event()
    closing, finish_close = asyncio.Event(), asyncio.Event()
    children = []

    async def spawn(cls, command, *, env=None):
        child = await original_spawn(command, env=env)
        children.append(child)
        if len(children) == 1 and stage == "spawn":
            entered.set()
            await finish_spawn.wait()
        return child

    async def receive(session, expected_id, timeout_ms, signal=None):
        if session.child is children[0] and stage == "handshake":
            entered.set()
            await asyncio.Event().wait()
        return await original_receive(session, expected_id, timeout_ms, signal)

    async def close(child):
        if child is children[0]:
            closing.set()
            await finish_close.wait()
        await original_close(child)
        if child is children[0]:
            if cleanup_result == "error":
                raise RuntimeError("cleanup failed")
            if cleanup_result == "cancelled":
                raise asyncio.CancelledError()

    async def launch(config):
        return await runtime()

    monkeypatch.setattr(OwnedProcess, "spawn", classmethod(spawn))
    monkeypatch.setattr(OwnedProcess, "_close", close)
    monkeypatch.setattr(RemotePythonRuntime, "_receive", receive)
    monkeypatch.setattr("app.plugin_session.launch_remote_python_runtime", launch)
    sessions = PluginSessions()
    connecting = asyncio.create_task(sessions.connect(CONFIG))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        entry = sessions._sessions["default"]
        startup = entry.startup
        assert startup is not None
        startup.cancel()
        await asyncio.sleep(0)
        startup.cancel()
        if stage == "spawn":
            await asyncio.sleep(0)
            assert not startup.done()
            assert children[0].process.returncode is None
        finish_spawn.set()
        await asyncio.wait_for(closing.wait(), 5)
        startup.cancel()
        await asyncio.sleep(0)
        assert not startup.done()
        assert sessions._sessions["default"] is entry
        assert children[0].process.returncode is None
        finish_close.set()
        if cleanup_result == "success":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(connecting, 5)
            assert await sessions.list_sessions() == []
            assert entry.cleanup is None
            assert children[0].process.returncode is not None
            assert (await sessions.connect(CONFIG))["connected"] is True
            await sessions.disconnect()
        else:
            with pytest.raises(RemotePythonStartupCleanupError) as caught:
                await asyncio.wait_for(connecting, 5)
            assert caught.value.code == "startup_cleanup_failed"
            assert (await sessions.list_sessions())[0]["connected"] is False
            with pytest.raises(RemotePythonStartupCleanupError):
                await sessions.connect(CONFIG, "default")
            assert len(children) == 1
    finally:
        finish_spawn.set()
        finish_close.set()
        await asyncio.gather(connecting, return_exceptions=True)
        # Reap fixtures even if an assertion fails or cleanup deliberately raises.
        for child in children:
            await original_close(child)


@pytest.mark.parametrize("spawn_error", [OSError, asyncio.CancelledError])
async def test_cancelled_startup_classifies_spawn_failure(monkeypatch, spawn_error):
    original_spawn = OwnedProcess.spawn
    entered, finish = asyncio.Event(), asyncio.Event()

    async def fail_spawn(*args, **kwargs):
        entered.set()
        await finish.wait()
        raise spawn_error("spawn failed")

    async def launch(config):
        return await runtime()

    monkeypatch.setattr(OwnedProcess, "spawn", fail_spawn)
    monkeypatch.setattr("app.plugin_session.launch_remote_python_runtime", launch)
    sessions = PluginSessions()
    connecting = asyncio.create_task(sessions.connect(CONFIG))
    await asyncio.wait_for(entered.wait(), 5)
    startup = sessions._sessions["default"].startup
    assert startup is not None
    startup.cancel()
    await asyncio.sleep(0)
    startup.cancel()
    finish.set()
    if spawn_error is OSError:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(connecting, 5)
        assert await sessions.list_sessions() == []
        monkeypatch.setattr(OwnedProcess, "spawn", original_spawn)
        try:
            assert (await sessions.connect(CONFIG))["connected"] is True
        finally:
            await sessions.disconnect()
    else:
        # A cancelled spawn never delivered a process handle. It cannot provide
        # the same cleanup guarantee as an ordinary process-creation failure.
        with pytest.raises(RemotePythonStartupCleanupError) as caught:
            await asyncio.wait_for(connecting, 5)
        assert caught.value.code == "startup_cleanup_failed"
        with pytest.raises(RemotePythonStartupCleanupError):
            await sessions.connect(CONFIG, "default")


async def test_real_worker_background_thread_keeps_emergency_release_after_exit(tmp_path):
    worker_path = tmp_path / "worker.py"
    helper, released = tmp_path / "release.py", tmp_path / "released"
    gate, acted = tmp_path / "gate", tmp_path / "acted"
    worker_path.write_text(
        "import sys\nfrom types import SimpleNamespace\n"
        f"sys.path.insert(0, {str(Path(__file__).parents[1])!r})\n"
        "from app.remote import worker\n"
        "worker.RemoteComputer = lambda: SimpleNamespace(\n"
        "    backend='fixture', target='fixture://test', width=64, height=48,\n"
        "    status=lambda: {'connected': True, 'inputHeld': False}, close=lambda: None)\n"
        "worker.main()\n", encoding="utf-8",
    )
    helper.write_text(
        f"from pathlib import Path\nPath({str(released)!r}).touch()\nprint('{{\"released\":true}}')\n",
        encoding="utf-8",
    )
    session = await launch_remote_python_runtime(
        CONFIG, worker_path=worker_path, release_helper_path=helper,
    )
    try:
        await session.execute(
            "import threading, time\nfrom pathlib import Path\n"
            "def late_action():\n"
            f"    while not Path({str(gate)!r}).exists(): time.sleep(0.01)\n"
            f"    Path({str(acted)!r}).touch()\n"
            "thread = threading.Thread(target=late_action, daemon=True)\nthread.start()"
        )
        # The action starts after the last worker reply. No further request
        # refreshes the parent's input state before shutdown.
        gate.touch()
        async def wait_for_late_action():
            while not acted.exists():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait_for_late_action(), 5)
    finally:
        gate.touch()
        await session.close()
    assert released.exists()
    assert session.child.process.returncode is not None


async def test_input_identities_arriving_after_reply_survive_close(tmp_path):
    helper, marker = tmp_path / "release.py", tmp_path / "identity-verified"
    helper.write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "data=json.loads(sys.stdin.readline())\n"
        "assert data['protocol']=='rdp' and data['identities']==[20320, 55357, 56832]\n"
        f"Path({str(marker)!r}).touch()\nprint('{{\"released\":true}}')\n", encoding="utf-8",
    )
    session = await runtime(release_helper_path=helper)
    try:
        # Simulate a late background input after the last clean response has
        # already been enqueued but before the request caller consumes it.
        original = session._receive
        async def receive(*args, **kwargs):
            result = await original(*args, **kwargs)
            session._pending_identities.update([20320, 55357, 56832])
            session._identity_generation += 1
            session._input_may_be_held = True
            return result
        session._receive = receive
        await session.execute("pass")
    finally:
        await session.close()
    assert marker.exists()


async def test_real_worker_identity_frames_reach_helper_before_cleanup(tmp_path):
    helper, marker = tmp_path / "release.py", tmp_path / "identity-verified"
    helper.write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "data=json.loads(sys.stdin.readline())\nassert data['identities']==[20320, 55357, 56832]\n"
        f"Path({str(marker)!r}).touch()\nprint('{{\"released\":true}}')\n", encoding="utf-8",
    )
    session = await runtime(release_helper_path=helper)
    try:
        await session.execute(
            "import sys, json\n"
            "sys.__stdout__.write(json.dumps({'kind':'input_identity','protocol':'rdp',"
            "'identities':[20320,55357,56832]})+'\\n'); sys.__stdout__.flush()\n"
            "input_held=True"
        )
        assert session._pending_identities == {20320, 55357, 56832}
    finally:
        await session.close()
    assert marker.exists()


async def test_late_empty_input_risk_frame_still_runs_release_helper(tmp_path: Path):
    helper, marker, gate = tmp_path / "release.py", tmp_path / "released", tmp_path / "emit-risk"
    helper.write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "data=json.loads(sys.stdin.readline())\n"
        "assert data['protocol']=='rdp' and data['identities']==[]\n"
        f"Path({str(marker)!r}).touch()\nprint('{{\"released\":true}}')\n",
        encoding="utf-8",
    )
    session = await runtime(release_helper_path=helper)
    try:
        await session.execute(
            "import sys, json, threading, time\nfrom pathlib import Path\n"
            "def late_risk():\n"
            f"    while not Path({str(gate)!r}).exists(): time.sleep(0.01)\n"
            "    sys.__stdout__.write(json.dumps({'kind':'input_identity','protocol':'rdp',"
            "'identities':[]})+'\\n'); sys.__stdout__.flush()\n"
            "threading.Thread(target=late_risk, daemon=True).start()"
        )
        assert session._input_may_be_held is False
        gate.touch()
        async def wait_for_risk():
            while session._identity_generation <= session._received_identity_generation:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait_for_risk(), 5)
        assert session._input_may_be_held is True
    finally:
        gate.touch()
        await session.close()
    assert marker.exists()


async def test_invalid_identity_frame_runs_known_release_then_keeps_cleanup_blocked(tmp_path: Path):
    helper, marker = tmp_path / "release.py", tmp_path / "release-attempted"
    helper.write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "data=json.loads(sys.stdin.readline())\nassert data['identities']==[20320]\n"
        f"Path({str(marker)!r}).touch()\nprint('{{\"released\":true}}')\n",
        encoding="utf-8",
    )
    session = await runtime(release_helper_path=helper)
    with pytest.raises(RemotePythonRuntimeError):
        await session.execute(
            "import sys, json\n"
            "sys.__stdout__.write(json.dumps({'kind':'input_identity','protocol':'rdp',"
            "'identities':[20320]})+'\\n'); sys.__stdout__.flush()\n"
            "sys.__stdout__.write(json.dumps({'kind':'input_identity','protocol':'vnc',"
            "'identities':[1]})+'\\n'); sys.__stdout__.flush()"
        )
    with pytest.raises(RemotePythonRuntimeError, match="full release cannot be confirmed") as failure:
        await session.close()
    assert failure.value.code == "input_release_failed"
    assert marker.exists()
