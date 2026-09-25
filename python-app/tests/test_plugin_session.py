from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.actions import ClickAction
from app.backends import RdpConfig
from app.plugin_session import PluginSessions
from app.remote.runtime import RemotePythonStartupCleanupError


@pytest.mark.asyncio
async def test_code_and_atomic_actions_share_one_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = SimpleNamespace(
        connected=True,
        close=AsyncMock(),
        status=AsyncMock(
            return_value={
                "connected": True,
                "backend": "rdp",
                "host": "target",
                "port": 3389,
                "width": 1440,
                "height": 900,
            }
        ),
        execute=AsyncMock(return_value={"output": [{"type": "input_text", "text": "42"}]}),
        run_actions=AsyncMock(return_value=b"\x89PNG\r\n\x1a\nfixture"),
        capture_screenshot=AsyncMock(return_value=b"\x89PNG\r\n\x1a\nfixture"),
    )
    launch = AsyncMock(return_value=runtime)
    monkeypatch.setattr("app.plugin_session.launch_remote_python_runtime", launch)
    session = PluginSessions()
    config = RdpConfig(host="target", port=3389, username="codex")

    status = await session.connect(config)
    result = await session.execute("counter = 41\nlog(counter + 1)")
    image = await session.run_actions([ClickAction(type="click", x=10, y=20)])
    await session.disconnect()

    assert status["connected"] is True
    assert status["session"] == "default"
    assert result["output"][0]["text"] == "42"
    assert image.startswith(b"\x89PNG")
    launch.assert_awaited_once_with(config)
    runtime.execute.assert_awaited_once()
    runtime.run_actions.assert_awaited_once_with(
        [{"type": "click", "x": 10, "y": 20, "button": "left", "count": 1}]
    )
    runtime.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_status_is_disconnected_without_worker() -> None:
    assert await PluginSessions().status() == {
        "connected": False,
        "backend": None,
        "host": None,
        "port": None,
        "width": None,
        "height": None,
    }


def _runtime(target: str) -> SimpleNamespace:
    return SimpleNamespace(
        connected=True,
        close=AsyncMock(),
        status=AsyncMock(
            return_value={
                "connected": True,
                "backend": "rdp",
                "host": target,
                "port": 3389,
                "width": 1440,
                "height": 900,
            }
        ),
        execute=AsyncMock(return_value={"output": [{"type": "input_text", "text": target}]}),
        run_actions=AsyncMock(return_value=b"\x89PNG\r\n\x1a\nfixture"),
        capture_screenshot=AsyncMock(return_value=b"\x89PNG\r\n\x1a\nfixture"),
    )


@pytest.mark.asyncio
async def test_named_sessions_route_independently_and_require_name_when_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    win = _runtime("win.example")
    linux = _runtime("linux.example")
    launch = AsyncMock(side_effect=[win, linux])
    monkeypatch.setattr("app.plugin_session.launch_remote_python_runtime", launch)
    session = PluginSessions()

    await session.connect(RdpConfig(host="win.example", port=3389, username="codex"), "win-dev")
    await session.connect(RdpConfig(host="linux.example", port=3389, username="codex"), "linux-test")

    with pytest.raises(RuntimeError, match="specify session"):
        await session.execute("log('ambiguous')")

    result = await session.execute("log('selected')", "linux-test")
    listed = await session.list_sessions()
    win.connected = False
    fallback = await session.execute("log('only connected')")
    await session.disconnect("win-dev")

    assert result["output"][0]["text"] == "linux.example"
    assert fallback["output"][0]["text"] == "linux.example"
    win.execute.assert_not_awaited()
    assert linux.execute.await_count == 2
    assert [item["session"] for item in listed] == ["linux-test", "win-dev"]
    assert all(item["busy"] is False for item in listed)
    win.close.assert_awaited_once()
    linux.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_different_named_sessions_can_execute_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _runtime("first")
    second = _runtime("second")
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release = asyncio.Event()

    async def execute_first(code: str) -> dict[str, object]:
        first_started.set()
        await release.wait()
        return {"output": []}

    async def execute_second(code: str) -> dict[str, object]:
        second_started.set()
        await release.wait()
        return {"output": []}

    first.execute = AsyncMock(side_effect=execute_first)
    second.execute = AsyncMock(side_effect=execute_second)
    monkeypatch.setattr(
        "app.plugin_session.launch_remote_python_runtime",
        AsyncMock(side_effect=[first, second]),
    )
    session = PluginSessions()
    config = RdpConfig(host="target", port=3389, username="codex")
    await session.connect(config, "first")
    await session.connect(config, "second")

    tasks = [
        asyncio.create_task(session.execute("pass", "first")),
        asyncio.create_task(session.execute("pass", "second")),
    ]
    await asyncio.wait_for(asyncio.gather(first_started.wait(), second_started.wait()), 0.2)
    listed = await asyncio.wait_for(session.list_sessions(), 0.05)
    assert [item["busy"] for item in listed] == [True, True]
    assert first.status.await_count == 1
    assert second.status.await_count == 1
    release.set()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_unnamed_connect_is_only_allowed_for_an_empty_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime("first")
    launch = AsyncMock(return_value=runtime)
    monkeypatch.setattr("app.plugin_session.launch_remote_python_runtime", launch)
    session = PluginSessions()
    first = RdpConfig(host="first", port=3389, username="codex")
    second = RdpConfig(host="second", port=3389, username="codex")

    await session.connect(first, "win-dev")
    with pytest.raises(RuntimeError, match="specify name"):
        await session.connect(second)

    launch.assert_awaited_once_with(first)
    runtime.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_first_anonymous_connects_reserve_default_once(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = PluginSessions()
    first = RdpConfig(host="first", port=3389, username="codex")
    second = RdpConfig(host="second", port=3389, username="codex")
    started, release = asyncio.Event(), asyncio.Event()

    # Yield after each registry inspection. With a separate name check and
    # registration, both callers can observe an empty registry here.
    original_lock = sessions._registry_lock

    class YieldingRegistryLock:
        async def __aenter__(self):
            await original_lock.acquire()

        async def __aexit__(self, *_args):
            original_lock.release()
            await asyncio.sleep(0)

    sessions._registry_lock = YieldingRegistryLock()  # type: ignore[assignment]

    async def launch(config):
        started.set()
        await release.wait()
        return _runtime(config.host)

    launcher = AsyncMock(side_effect=launch)
    monkeypatch.setattr("app.plugin_session.launch_remote_python_runtime", launcher)
    barrier = asyncio.Event()

    async def connect(config):
        await barrier.wait()
        return await sessions.connect(config)

    calls = [asyncio.create_task(connect(config)) for config in (first, second)]
    barrier.set()
    try:
        await asyncio.wait_for(started.wait(), 5)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*calls, return_exceptions=True), 5)
        success = [result for result in results if isinstance(result, dict)]
        rejected = [result for result in results if isinstance(result, RuntimeError)]
        assert len(success) == len(rejected) == 1
        assert "specify name" in str(rejected[0])
        assert launcher.await_count == 1
        assert (await sessions.status())["host"] == success[0]["host"]
        await sessions.disconnect()
    finally:
        release.set()
        await asyncio.gather(*calls, return_exceptions=True)


@pytest.mark.asyncio
async def test_disconnect_removes_the_entire_session_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.plugin_session.launch_remote_python_runtime",
        AsyncMock(return_value=_runtime("target")),
    )
    session = PluginSessions()
    await session.connect(RdpConfig(host="target", port=3389, username="codex"), "temporary")

    await session.disconnect("temporary")

    assert await session.list_sessions() == []
    assert session._sessions == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_disconnect", [False, True])
async def test_same_name_connect_waits_for_disconnect_cleanup(monkeypatch, cancel_disconnect):
    old, new, other = _runtime("old"), _runtime("new"), _runtime("other")
    entered, release = asyncio.Event(), asyncio.Event()
    events = []

    async def close():
        entered.set()
        await release.wait()
        events.append("old released")

    async def launch(config):
        events.append("launch " + config.host)
        return {"old": old, "new": new, "other": other}[config.host]

    old.close = AsyncMock(side_effect=close)
    monkeypatch.setattr("app.plugin_session.launch_remote_python_runtime", launch)
    sessions = PluginSessions()
    await sessions.connect(RdpConfig(host="old", port=3389, username="fixture"), "x")
    disconnect = asyncio.create_task(sessions.disconnect("x"))
    await entered.wait()
    original = sessions._sessions["x"]
    if cancel_disconnect:
        disconnect.cancel()
        with pytest.raises(asyncio.CancelledError):
            await disconnect
    replacement = asyncio.create_task(sessions.connect(RdpConfig(host="new", port=3389, username="fixture"), "x"))
    try:
        await sessions.connect(RdpConfig(host="other", port=3389, username="fixture"), "other")
        await asyncio.sleep(0)
        assert sessions._sessions["x"] is original
        assert "launch new" not in events
        listed = {item["session"]: item for item in await sessions.list_sessions()}
        assert listed["x"]["connected"] is False
        assert listed["x"]["busy"] is True
    finally:
        release.set()
        await asyncio.gather(disconnect, replacement, return_exceptions=True)
    assert (await replacement)["host"] == "new"
    assert events.index("old released") < events.index("launch new")
    old.close.assert_awaited_once()
    await sessions.disconnect("x")
    await sessions.disconnect("other")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["disconnect", "replace"])
async def test_failed_cleanup_keeps_name_blocked(monkeypatch, operation):
    old = _runtime("old")
    old.close.side_effect = RuntimeError("release failed")
    launch = AsyncMock(return_value=old)
    monkeypatch.setattr("app.plugin_session.launch_remote_python_runtime", launch)
    sessions = PluginSessions()
    config = RdpConfig(host="old", port=3389, username="fixture")
    await sessions.connect(config, "x")
    with pytest.raises(RuntimeError, match="release failed"):
        if operation == "disconnect":
            await sessions.disconnect("x")
        else:
            await sessions.connect(config, "x")
    with pytest.raises(RuntimeError, match="release failed"):
        await sessions.connect(config, "x")
    assert (await sessions.list_sessions())[0]["connected"] is False
    launch.assert_awaited_once()
    old.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_launch_status_failure_reports_cleanup_failure_immediately(monkeypatch):
    runtime = _runtime("target")
    runtime.status.side_effect = RuntimeError("status failed")
    runtime.close.side_effect = RuntimeError("release failed")
    launch = AsyncMock(return_value=runtime)
    monkeypatch.setattr("app.plugin_session.launch_remote_python_runtime", launch)
    sessions = PluginSessions()
    config = RdpConfig(host="target", port=3389, username="fixture")

    with pytest.raises(RuntimeError, match="release failed") as caught:
        await sessions.connect(config, "x")

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert str(caught.value.__cause__) == "status failed"
    assert (await sessions.list_sessions())[0]["connected"] is False
    with pytest.raises(RuntimeError, match="release failed"):
        await sessions.connect(config, "x")
    launch.assert_awaited_once()
    runtime.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeated_cancellation_during_startup_cannot_orphan_worker(monkeypatch):
    old, new = _runtime("old"), _runtime("new")
    started, finish_start = asyncio.Event(), asyncio.Event()
    closing, finish_close = asyncio.Event(), asyncio.Event()
    events = []

    async def launch(config):
        events.append("launch " + config.host)
        if config.host == "old":
            started.set()
            await finish_start.wait()
            return old
        return new

    async def close():
        closing.set()
        await finish_close.wait()
        events.append("old closed")

    old.close = AsyncMock(side_effect=close)
    monkeypatch.setattr("app.plugin_session.launch_remote_python_runtime", launch)
    sessions = PluginSessions()
    original = asyncio.create_task(sessions.connect(RdpConfig(host="old", port=3389, username="fixture"), "x"))
    await started.wait()
    original.cancel()
    await asyncio.sleep(0)
    original.cancel()
    with pytest.raises(asyncio.CancelledError):
        await original
    replacement = asyncio.create_task(sessions.connect(RdpConfig(host="new", port=3389, username="fixture"), "x"))
    try:
        await asyncio.sleep(0)
        assert events == ["launch old"]
        finish_start.set()
        await asyncio.wait_for(closing.wait(), 1)
        assert events == ["launch old"]
        assert (await sessions.list_sessions())[0]["connected"] is False
    finally:
        finish_start.set()
        finish_close.set()
        await replacement
    assert events == ["launch old", "old closed", "launch new"]
    old.close.assert_awaited_once()
    await sessions.disconnect("x")


@pytest.mark.asyncio
async def test_failed_startup_allows_a_clean_retry(monkeypatch):
    launch = AsyncMock(side_effect=[RuntimeError("handshake failed"), _runtime("new")])
    monkeypatch.setattr("app.plugin_session.launch_remote_python_runtime", launch)
    sessions = PluginSessions()
    config = RdpConfig(host="new", port=3389, username="fixture")
    with pytest.raises(RuntimeError, match="handshake failed"):
        await sessions.connect(config, "x")
    assert await sessions.list_sessions() == []
    assert (await sessions.connect(config, "x"))["connected"] is True
    await sessions.disconnect("x")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [None, "x"])
async def test_cancelled_startup_task_allows_clean_retry(monkeypatch, name):
    launch = AsyncMock(side_effect=[asyncio.CancelledError(), _runtime("new")])
    monkeypatch.setattr("app.plugin_session.launch_remote_python_runtime", launch)
    sessions = PluginSessions()
    config = RdpConfig(host="new", port=3389, username="fixture")
    with pytest.raises(asyncio.CancelledError):
        await sessions.connect(config, name)
    assert await sessions.list_sessions() == []
    assert (await sessions.connect(config, name))["connected"] is True
    await sessions.disconnect(name)


@pytest.mark.asyncio
async def test_startup_cancelled_before_first_step_allows_retry(monkeypatch):
    launch = AsyncMock(return_value=_runtime("new"))
    monkeypatch.setattr("app.plugin_session.launch_remote_python_runtime", launch)
    sessions = PluginSessions()
    config = RdpConfig(host="new", port=3389, username="fixture")
    connecting = asyncio.create_task(sessions.connect(config))
    await asyncio.sleep(0)
    startup = sessions._sessions["default"].startup
    assert startup is not None
    startup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connecting
    launch.assert_not_awaited()
    assert await sessions.list_sessions() == []
    assert (await sessions.connect(config))["connected"] is True
    await sessions.disconnect()


@pytest.mark.asyncio
async def test_cancelled_startup_is_cleaned_without_another_tool_call(monkeypatch):
    runtime, new = _runtime("old"), _runtime("new")
    started, finish = asyncio.Event(), asyncio.Event()

    async def launch(config):
        if config.host == "new":
            return new
        started.set()
        await finish.wait()
        return runtime

    monkeypatch.setattr("app.plugin_session.launch_remote_python_runtime", launch)
    sessions = PluginSessions()
    connecting = asyncio.create_task(sessions.connect(RdpConfig(host="old", port=3389, username="fixture")))
    await started.wait()
    connecting.cancel()
    await asyncio.sleep(0)
    connecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connecting
    cleanup = sessions._sessions["default"].cleanup
    assert cleanup is not None
    finish.set()
    await asyncio.wait_for(asyncio.shield(cleanup), 1)
    runtime.close.assert_awaited_once()
    assert await sessions.list_sessions() == []
    assert (await sessions.connect(RdpConfig(host="new", port=3389, username="fixture")))["host"] == "new"
    await sessions.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_twice", [False, True])
@pytest.mark.parametrize("startup_error", [RuntimeError, asyncio.CancelledError])
async def test_cancelled_startup_failure_allows_clean_retry(monkeypatch, cancel_twice, startup_error):
    started, finish = asyncio.Event(), asyncio.Event()
    new = _runtime("new")

    async def launch(config):
        if config.host == "new":
            return new
        started.set()
        await finish.wait()
        raise startup_error("handshake failed")

    monkeypatch.setattr("app.plugin_session.launch_remote_python_runtime", launch)
    sessions = PluginSessions()
    connecting = asyncio.create_task(sessions.connect(RdpConfig(host="old", port=3389, username="fixture"), "x"))
    await started.wait()
    connecting.cancel()
    await asyncio.sleep(0)
    cleanup = sessions._sessions["x"].cleanup
    assert cleanup is not None
    if cancel_twice:
        connecting.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await connecting
    await asyncio.wait_for(asyncio.shield(cleanup), 1)
    assert await sessions.list_sessions() == []
    assert (await sessions.connect(RdpConfig(host="new", port=3389, username="fixture"), "x"))["host"] == "new"
    await sessions.disconnect("x")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["disconnect", "replace"])
async def test_cancelled_close_retires_registry_without_another_caller(monkeypatch, operation):
    old, new = _runtime("old"), _runtime("new")
    closing, finish = asyncio.Event(), asyncio.Event()

    async def close():
        closing.set()
        await finish.wait()

    old.close = AsyncMock(side_effect=close)
    launch = AsyncMock(side_effect=[old, new])
    monkeypatch.setattr("app.plugin_session.launch_remote_python_runtime", launch)
    sessions = PluginSessions()
    config = RdpConfig(host="target", port=3389, username="fixture")
    await sessions.connect(config)
    operation_task = asyncio.create_task(
        sessions.disconnect() if operation == "disconnect" else sessions.connect(config, "default")
    )
    await closing.wait()
    entry = sessions._sessions["default"]
    cleanup = entry.cleanup
    assert cleanup is not None
    operation_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation_task
    finish.set()
    await asyncio.wait_for(asyncio.shield(cleanup), 1)
    assert await sessions.list_sessions() == []
    assert entry.cleanup is None
    old.close.assert_awaited_once()
    launch.assert_awaited_once()
    assert (await sessions.connect(config))["host"] == "new"
    await sessions.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_connect", [False, True])
async def test_startup_cleanup_failure_keeps_name_blocked(monkeypatch, cancel_connect):
    started, finish = asyncio.Event(), asyncio.Event()

    async def fail_startup(config):
        started.set()
        await finish.wait()
        raise RemotePythonStartupCleanupError("worker cleanup failed", "startup_cleanup_failed")

    launch = AsyncMock(side_effect=fail_startup)
    monkeypatch.setattr("app.plugin_session.launch_remote_python_runtime", launch)
    sessions = PluginSessions()
    config = RdpConfig(host="target", port=3389, username="fixture")
    connecting = asyncio.create_task(sessions.connect(config, "x"))
    await started.wait()
    if cancel_connect:
        connecting.cancel()
        await asyncio.sleep(0)
    finish.set()
    with pytest.raises(asyncio.CancelledError if cancel_connect else RemotePythonStartupCleanupError):
        await connecting
    with pytest.raises(RemotePythonStartupCleanupError, match="worker cleanup failed"):
        await sessions.connect(config, "x")
    assert (await sessions.list_sessions())[0]["connected"] is False
    launch.assert_awaited_once()


@pytest.mark.asyncio
async def test_queued_replacements_never_overlap_or_remove_the_live_entry(monkeypatch):
    closing, finish = asyncio.Event(), asyncio.Event()
    live = set()
    runtimes = []

    async def launch(config):
        assert not live
        live.add(config.host)
        worker = _runtime(config.host)

        async def close():
            if config.host == "old":
                closing.set()
                await finish.wait()
            live.remove(config.host)

        worker.close = AsyncMock(side_effect=close)
        runtimes.append(worker)
        return worker

    monkeypatch.setattr("app.plugin_session.launch_remote_python_runtime", launch)
    sessions = PluginSessions()
    await sessions.connect(RdpConfig(host="old", port=3389, username="fixture"), "x")
    disconnect = asyncio.create_task(sessions.disconnect("x"))
    await closing.wait()
    replacements = [
        asyncio.create_task(sessions.connect(RdpConfig(host=host, port=3389, username="fixture"), "x"))
        for host in ("first", "second", "third")
    ]
    await asyncio.sleep(0)
    finish.set()
    await asyncio.wait_for(asyncio.gather(disconnect, *replacements), 1)
    assert len(live) == 1
    assert (await sessions.status("x"))["host"] in live
    assert len(runtimes) == 4
    await sessions.disconnect("x")
    assert live == set()
    for worker in runtimes:
        worker.close.assert_awaited_once()
