"""Run-scoped lab setup, execution, screenshots, and awaited teardown."""
import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import RunnerCoreError
from .lab_catalog import instructions, list_scenarios
from .processes import assert_active


@dataclass
class RunExecutionContext:
    detail: dict
    signal: asyncio.Event
    screenshot_directory: Path
    emit_event: Callable[..., Awaitable]
    capture_screenshot: Callable[..., Awaitable]
    sync_browser_state: Callable[..., Awaitable]
    complete_run: Callable[..., Awaitable]


async def delay(milliseconds: int, signal: asyncio.Event) -> None:
    assert_active(signal)
    try:
        await asyncio.wait_for(signal.wait(), milliseconds / 1000)
    except TimeoutError:
        return
    raise asyncio.CancelledError("Run aborted.")


async def hold_browser(context: RunExecutionContext) -> None:
    if context.detail["run"]["browserMode"] != "headful":
        return
    prompt = context.detail["run"]["prompt"].lower()
    match = re.search(r"wait\s+(\d+)\s*(?:seconds?|secs?|s)\b|keep(?:\s+it)?\s+open\s+for\s+(\d+)\s*(?:seconds?|secs?|s)\b", prompt)
    if match:
        milliseconds = min(15000, max(1000, int(match[1] or match[2]) * 1000))
    else:
        milliseconds = 5000 if any(word in prompt for word in ("wait", "hold open", "keep open", "stay open")) else 3500
    await context.emit_event({"type": "run_progress", "level": "pending", "detail": f"{milliseconds / 1000:g}s operator review window",
                              "message": "Holding the headful browser session open before teardown."})
    await delay(milliseconds, context.signal)


async def execute_scenario(context: RunExecutionContext) -> None:
    from .backends import load_backend_config
    from .lab_server import start_guest_workspace_lab_server
    from .remote_runtime import launch_remote_session
    from .responses_loop import create_default_responses_client, run_responses_code_loop

    run = context.detail["run"]
    if not any(item["id"] == run["scenarioId"] and item["labId"] == run["labId"] for item in list_scenarios()):
        raise RunnerCoreError(f"Unsupported public scenario: {run['scenarioId']}", code="unsupported_scenario", status_code=404,
                              hint="Use one of the public scenarios from /api/scenarios.")
    client = create_default_responses_client()
    lab_id = run["labId"]
    lab_server = None
    session = None

    async def cleanup() -> None:
        errors = []
        for resource in (session, lab_server, client):
            if resource is not None:
                try:
                    await resource.close()
                except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - attempt every owned resource cleanup
                    errors.append(error)
        if errors:
            release_failed = any(getattr(error, "code", "") in ("input_release_failed", "input_release_timeout") for error in errors)
            raise RunnerCoreError("Run cleanup failed: " + "; ".join(str(error) for error in errors),
                                  code="remote_cleanup_failed" if release_failed else "cleanup_failed",
                                  hint="Disconnect the remote session, then restart the runner before starting another run." if release_failed else None)

    try:
        assert_active(context.signal)
        await context.emit_event({"type": "run_progress", "level": "ok", "detail": run["model"],
                                  "message": f"Using the persistent remote Python code loop for the {lab_id} scenario."})
        config = load_backend_config()
        lab_server = await start_guest_workspace_lab_server(
            workspace_path=context.detail["workspacePath"],
            remote_host=config.host,
            remote_port=config.port,
        )
        lab_url = lab_server.url_for("index.html")
        await context.emit_event({"type": "lab_started", "level": "ok", "detail": lab_url,
                                  "message": "Guest-reachable lab server booted from the mutable workspace."})
        session = await launch_remote_session(
            config, context.screenshot_directory, signal=context.signal
        )
        assert_active(context.signal)
        await session.navigate(lab_url)
        try:
            await lab_server.wait_for_index()
        except TimeoutError as error:
            raise RunnerCoreError(
                "The remote guest did not load the selected lab.",
                code="guest_lab_unreachable",
                hint=(
                    "Allow the guest to reach the run-scoped lab server, or set "
                    "REMOTE_COMPUTER_LAB_HOST and REMOTE_COMPUTER_LAB_BIND_HOST to a reachable host interface."
                ),
            ) from error
        await context.sync_browser_state(session)
        await context.emit_event({"type": "browser_session_started", "level": "ok", "detail": session.target_label,
                                  "message": "Remote computer session connected and bound to the run."})
        await context.emit_event({"type": "browser_navigated", "level": "ok", "detail": lab_url,
                                  "message": f"Remote guest navigated to the {lab_id} lab."})
        await context.capture_screenshot(session, f"{lab_id}-loaded")
        result = await run_responses_code_loop(
            context=context,
            session=session,
            instructions=instructions(lab_id, lab_url),
            max_response_turns=run["maxResponseTurns"],
            client=client,
        )
        await context.capture_screenshot(session, f"{lab_id}-final")
        await hold_browser(context)
        await context.complete_run(notes=result["notes"])
    finally:
        # Once teardown starts, a second cancellation must not abandon resources.
        teardown = asyncio.create_task(cleanup())
        while True:
            try:
                await asyncio.shield(teardown)
                break
            except asyncio.CancelledError:
                if teardown.done():
                    await teardown
                    break
