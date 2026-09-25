import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.errors import RunnerCoreError
from app.scenario_runtime import RunExecutionContext, execute_scenario


@pytest.fixture
def flow(monkeypatch, tmp_path):
    client = SimpleNamespace(close=AsyncMock())
    session = SimpleNamespace(
        close=AsyncMock(),
        navigate=AsyncMock(),
        current_url="rdp://target:3389",
        target_label="RDP target:3389",
        execution=SimpleNamespace(target="rdp://target:3389"),
    )
    launch_remote = AsyncMock(return_value=session)
    lab_server = SimpleNamespace(
        close=AsyncMock(),
        url_for=lambda path: f"http://192.0.2.1:4567/{path}",
        wait_for_index=AsyncMock(),
    )
    start_lab = AsyncMock(return_value=lab_server)
    run_loop = AsyncMock(return_value={"notes": ["model final"]})
    config = SimpleNamespace(kind="rdp", host="target", port=3389)
    monkeypatch.setattr("app.backends.load_backend_config", lambda: config)
    monkeypatch.setattr("app.responses_loop.create_default_responses_client", lambda: client)
    monkeypatch.setattr("app.responses_loop.run_responses_code_loop", run_loop)
    monkeypatch.setattr("app.remote_runtime.launch_remote_session", launch_remote)
    monkeypatch.setattr("app.lab_server.start_guest_workspace_lab_server", start_lab)
    monkeypatch.setattr("app.scenario_runtime.hold_browser", AsyncMock())

    def context(lab="kanban", prompt="Do the task"):
        scenario = {
            "kanban": "kanban-reprioritize-sprint",
            "paint": "paint-draw-poster",
            "booking": "booking-complete-reservation",
        }[lab]
        return RunExecutionContext(
            detail={
                "run": {
                    "labId": lab,
                    "scenarioId": scenario,
                    "browserMode": "headful",
                    "prompt": prompt,
                    "model": "fixture",
                    "maxResponseTurns": 24,
                },
                "workspacePath": str(tmp_path),
            },
            signal=asyncio.Event(),
            screenshot_directory=Path(tmp_path),
            emit_event=AsyncMock(),
            capture_screenshot=AsyncMock(),
            sync_browser_state=AsyncMock(),
            complete_run=AsyncMock(),
        )

    return SimpleNamespace(
        context=context,
        session=session,
        client=client,
        lab_server=lab_server,
        start_lab=start_lab,
        launch_remote=launch_remote,
        run_loop=run_loop,
    )


@pytest.mark.parametrize("lab", ["kanban", "paint", "booking"])
async def test_all_scenarios_finish_only_with_awaited_resource_cleanup(flow, lab):
    context = flow.context(lab)
    await execute_scenario(context)
    flow.session.close.assert_awaited_once()
    flow.lab_server.close.assert_awaited_once()
    flow.client.close.assert_awaited_once()
    context.complete_run.assert_awaited_once_with(notes=["model final"])
    assert context.capture_screenshot.call_args.args[1] == f"{lab}-final"
    assert flow.run_loop.await_args.kwargs["session"] is flow.session
    assert "exec_py" in flow.run_loop.await_args.kwargs["instructions"]
    flow.session.navigate.assert_awaited_once_with("http://192.0.2.1:4567/index.html")
    flow.lab_server.wait_for_index.assert_awaited_once()


async def test_unknown_scenario_fails_before_remote_connection(flow):
    context = flow.context()
    context.detail["run"]["scenarioId"] = "missing"
    with pytest.raises(RunnerCoreError) as caught:
        await execute_scenario(context)
    assert caught.value.code == "unsupported_scenario"
    flow.launch_remote.assert_not_awaited()


async def test_remote_start_failure_closes_responses_client(flow):
    flow.launch_remote.side_effect = RuntimeError("Remote connection failed")
    with pytest.raises(RuntimeError, match="Remote connection"):
        await execute_scenario(flow.context())
    flow.lab_server.close.assert_awaited_once()
    flow.client.close.assert_awaited_once()


async def test_cleanup_failure_still_closes_every_owned_resource(flow):
    flow.session.close.side_effect = RuntimeError("Disconnect failed")
    with pytest.raises(RunnerCoreError) as caught:
        await execute_scenario(flow.context())
    assert caught.value.code == "cleanup_failed"
    flow.session.close.assert_awaited_once()
    flow.lab_server.close.assert_awaited_once()
    flow.client.close.assert_awaited_once()


@pytest.mark.parametrize("code", ["input_release_failed", "input_release_timeout"])
async def test_input_release_failure_is_reported_as_remote_cleanup_failure(flow, code):
    from app.remote.runtime import RemotePythonRuntimeError

    flow.session.close.side_effect = RemotePythonRuntimeError("Release failed", code=code)
    with pytest.raises(RunnerCoreError) as caught:
        await execute_scenario(flow.context())
    assert caught.value.code == "remote_cleanup_failed"
    flow.lab_server.close.assert_awaited_once()
    flow.client.close.assert_awaited_once()


async def test_guest_must_request_selected_lab_before_model_loop(flow):
    flow.lab_server.wait_for_index.side_effect = TimeoutError()

    with pytest.raises(RunnerCoreError) as caught:
        await execute_scenario(flow.context())

    assert caught.value.code == "guest_lab_unreachable"
    flow.run_loop.assert_not_awaited()
    flow.session.close.assert_awaited_once()
    flow.lab_server.close.assert_awaited_once()


async def test_repeated_cancellation_does_not_abandon_cleanup(flow):
    entered, release = asyncio.Event(), asyncio.Event()
    async def close():
        entered.set()
        await release.wait()

    flow.session.close.side_effect = close
    task = asyncio.create_task(execute_scenario(flow.context()))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    await task
    flow.session.close.assert_awaited_once()


async def test_paint_completion_does_not_export_remote_content(flow):
    context = flow.context("paint")
    await execute_scenario(context)
    context.complete_run.assert_awaited_once_with(notes=["model final"])
    assert context.capture_screenshot.call_args.args[1] == "paint-final"
    assert not (Path(context.detail["workspacePath"]) / "artwork").exists()
