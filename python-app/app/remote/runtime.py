"""Own the persistent remote Python worker and its cancellation boundaries."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

from ..backends import BackendConfig, RdpConfig, VncConfig
from ..processes import OwnedProcess, assert_active, await_active, child_environment, drain_tail

logger = logging.getLogger(__name__)


class RemotePythonRuntimeError(RuntimeError):
    def __init__(self, error: Any, code: str = "python_operation_failed") -> None:
        if isinstance(error, dict):
            code = error.get("code", code)
            error = error.get("message", str(error))
        super().__init__(str(error))
        self.code = code


def read_output(result: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(result.get("output"), list):
        raise RemotePythonRuntimeError("Python returned invalid tool output.")
    output = []
    for item in result["output"]:
        if isinstance(item, dict):
            if item.get("type") == "input_text" and isinstance(item.get("text"), str):
                output.append({"type": "input_text", "text": item["text"]})
                continue
            if (
                item.get("type") == "input_image"
                and isinstance(item.get("image_url"), str)
                and item["image_url"].startswith("data:image/png;base64,")
            ):
                output.append({"type": "input_image", "image_url": item["image_url"], "detail": "original"})
                continue
        raise RemotePythonRuntimeError("Python returned an unsupported output item.")
    return output


def _config_environment(config: BackendConfig) -> dict[str, str]:
    values: dict[str, str] = {"REMOTE_COMPUTER_BACKEND": config.kind}
    if isinstance(config, RdpConfig):
        values.update(
            {
                "REMOTE_COMPUTER_RDP_HOST": config.host,
                "REMOTE_COMPUTER_RDP_PORT": str(config.port),
                "REMOTE_COMPUTER_RDP_USERNAME": config.username,
                "REMOTE_COMPUTER_RDP_PASSWORD": config.password,
                "REMOTE_COMPUTER_RDP_DOMAIN": config.domain,
                "REMOTE_COMPUTER_RDP_WIDTH": str(config.width),
                "REMOTE_COMPUTER_RDP_HEIGHT": str(config.height),
                "REMOTE_COMPUTER_RDP_INSECURE_TLS": str(config.insecure_tls).lower(),
            }
        )
    elif isinstance(config, VncConfig):
        values.update(
            {
                "REMOTE_COMPUTER_VNC_HOST": config.host,
                "REMOTE_COMPUTER_VNC_PORT": str(config.port),
                "REMOTE_COMPUTER_VNC_PASSWORD": config.password,
                "REMOTE_COMPUTER_VNC_SHARED": str(config.shared).lower(),
            }
        )
    return values


class RemotePythonRuntime:
    def __init__(
        self,
        child: OwnedProcess,
        *,
        python_path: str,
        release_helper_path: str,
        environment: dict[str, str],
        signal: asyncio.Event | None,
        execution_timeout_ms: int,
        release_timeout_ms: int,
    ) -> None:
        self.child = child
        self.platform = "remote"
        self.backend = "unknown"
        self.target = "remote://unknown"
        self.width = 1
        self.height = 1
        self.python_path = python_path
        self.release_helper_path = release_helper_path
        self.environment = environment
        self.signal = signal
        self.execution_timeout_ms = execution_timeout_ms
        self.release_timeout_ms = release_timeout_ms
        self._sequence = 0
        self._busy = False
        self._input_may_be_held = False
        self._reported_input_held = False
        self._closing: asyncio.Task[None] | None = None
        self._stderr = asyncio.create_task(drain_tail(child.stderr))
        self._watcher = asyncio.create_task(self._watch())

    async def _watch(self) -> None:
        exited = asyncio.create_task(self.child.wait())
        cancelled = asyncio.create_task(self.signal.wait()) if self.signal is not None else None
        try:
            await asyncio.wait([exited] + ([cancelled] if cancelled else []), return_when=asyncio.FIRST_COMPLETED)
            with contextlib.suppress(Exception):
                await self.close()
        finally:
            for task in (exited, cancelled):
                if task is not None:
                    task.cancel()

    async def _receive(
        self,
        expected_id: int | None,
        timeout_ms: int,
        signal: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        line_task = asyncio.create_task(self.child.stdout.readline())
        exit_task = asyncio.create_task(self.child.wait())
        try:
            async def read_line() -> bytes:
                await asyncio.wait((line_task, exit_task), return_when=asyncio.FIRST_COMPLETED)
                if line_task.done():
                    line = await line_task
                    if line:
                        return line
                if exit_task.done():
                    raise RemotePythonRuntimeError("Python worker exited.")
                raise RemotePythonRuntimeError("Python worker closed its output.")

            try:
                line = await asyncio.wait_for(await_active(read_line(), signal or self.signal), timeout_ms / 1000)
            except TimeoutError as error:
                raise RemotePythonRuntimeError(
                    f"Python execution exceeded {timeout_ms}ms. Start a new run."
                ) from error
            except (ValueError, asyncio.LimitOverrunError) as error:
                raise RemotePythonRuntimeError("Python output exceeds 12 MiB.") from error
            if len(line) > 12 * 1024 * 1024:
                raise RemotePythonRuntimeError("Python output exceeds 12 MiB.")
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise TypeError("Invalid response.")
                if expected_id is not None and value.get("id") != expected_id:
                    raise ValueError("Unexpected response ID.")
                if expected_id is None and "id" in value:
                    raise ValueError("Unexpected startup response ID.")
            except (TypeError, ValueError, UnicodeError) as error:
                raise RemotePythonRuntimeError("Python worker returned invalid JSON or response ID.") from error
            if isinstance(value.get("inputHeld"), bool):
                self._reported_input_held = value["inputHeld"]
            if "error" in value:
                raise RemotePythonRuntimeError(value["error"])
            return value
        finally:
            for task in (line_task, exit_task):
                task.cancel()
            await asyncio.gather(line_task, exit_task, return_exceptions=True)

    async def _request(
        self,
        operation: str,
        *,
        signal: asyncio.Event | None = None,
        input_may_be_held: bool = False,
        **payload: Any,
    ) -> dict[str, Any]:
        if self._closing is not None:
            raise RemotePythonRuntimeError("Python session closed.")
        if self._busy:
            raise RemotePythonRuntimeError("A Python operation is already running.")
        self._busy = True
        try:
            assert_active(self.signal)
            assert_active(signal)
            self._sequence += 1
            self._input_may_be_held = self._input_may_be_held or input_may_be_held
            request = json.dumps({"id": self._sequence, "operation": operation, **payload}) + "\n"
            await await_active(self.child.write(request.encode()), signal or self.signal)
            value = await self._receive(self._sequence, self.execution_timeout_ms, signal)
            assert_active(self.signal)
            assert_active(signal)
            self._input_may_be_held = self._reported_input_held
            return value
        except BaseException:
            try:
                await self.close()
            except BaseException:  # Preserve the execution or cancellation failure.
                logger.warning("Remote Python cleanup after operation failure also failed", exc_info=True)
            raise
        finally:
            self._busy = False

    async def execute(self, code: str, signal: asyncio.Event | None = None) -> dict[str, Any]:
        if not isinstance(code, str) or not code.strip() or len(code.encode()) > 64 * 1024:
            raise RemotePythonRuntimeError("Python code must be nonempty and at most 64 KiB.")
        return {"output": read_output(await self._request("execute", code=code, signal=signal, input_may_be_held=True))}

    async def capture_screenshot(self) -> bytes:
        import base64

        output = read_output(await self._request("screenshot"))
        if len(output) != 1 or output[0]["type"] != "input_image":
            raise RemotePythonRuntimeError("Remote worker returned an invalid screenshot.")
        return base64.b64decode(output[0]["image_url"].removeprefix("data:image/png;base64,"), validate=True)

    async def run_actions(self, actions: list[dict[str, Any]]) -> bytes:
        import base64

        output = read_output(
            await self._request("actions", actions=actions, input_may_be_held=True)
        )
        if len(output) != 1 or output[0]["type"] != "input_image":
            raise RemotePythonRuntimeError("Remote worker returned an invalid action screenshot.")
        return base64.b64decode(output[0]["image_url"].removeprefix("data:image/png;base64,"), validate=True)

    async def status(self) -> dict[str, Any]:
        result = await self._request("status")
        data = result.get("data")
        if not isinstance(data, dict):
            raise RemotePythonRuntimeError("Remote worker returned invalid status.")
        return data

    @property
    def connected(self) -> bool:
        return self._closing is None and self.child.process.returncode is None

    async def _release_inputs(self) -> None:
        try:
            helper = await OwnedProcess.spawn(
                [self.python_path, "-I", "-u", self.release_helper_path],
                env=self.environment,
            )
        except Exception as error:
            raise RemotePythonRuntimeError(f"Could not start input release: {error}", "input_release_failed") from error
        stdout = asyncio.create_task(drain_tail(helper.stdout))
        stderr = asyncio.create_task(drain_tail(helper.stderr))
        try:
            try:
                await asyncio.wait_for(helper.wait(), self.release_timeout_ms / 1000)
            except TimeoutError as error:
                raise RemotePythonRuntimeError(
                    f"Input release exceeded {self.release_timeout_ms}ms.",
                    "input_release_timeout",
                ) from error
            await asyncio.sleep(0)
            await helper.close()
            result = json.loads(await stdout)
            if helper.process.returncode != 0 or result.get("released") is not True:
                raise ValueError(result.get("error", "Release was not acknowledged."))
        except RemotePythonRuntimeError:
            raise
        except Exception as error:
            raise RemotePythonRuntimeError(f"Input release failed: {error}", "input_release_failed") from error
        finally:
            await helper.close()
            await asyncio.gather(stdout, stderr, return_exceptions=True)

    async def _close(self) -> None:
        failure: Exception | None = None
        try:
            await self.child.close()
        except Exception as error:  # noqa: BLE001 - release must still be attempted
            failure = error
        finally:
            await self._stderr
        if self._input_may_be_held:
            await self._release_inputs()
        if failure is not None:
            raise failure

    async def close(self) -> None:
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        await asyncio.shield(self._closing)


async def launch_remote_python_runtime(
    config: BackendConfig,
    *,
    signal: asyncio.Event | None = None,
    execution_timeout_ms: int = 60_000,
    startup_timeout_ms: int = 60_000,
    python_path: str | None = None,
    worker_path: str | Path | None = None,
    release_helper_path: str | Path | None = None,
    release_timeout_ms: int = 15_000,
) -> RemotePythonRuntime:
    assert_active(signal)
    python_path = python_path or sys.executable
    directory = Path(__file__).parent
    environment = child_environment()
    environment.update(_config_environment(config))
    try:
        child = await OwnedProcess.spawn(
            [python_path, "-u", str(worker_path or directory / "worker.py")],
            env=environment,
        )
    except Exception as error:
        raise RemotePythonRuntimeError(
            f"Could not start remote Python: {error}. Run uv sync and restart the runner."
        ) from error
    runtime = RemotePythonRuntime(
        child,
        python_path=python_path,
        release_helper_path=str(release_helper_path or directory / "release_inputs.py"),
        environment=environment,
        signal=signal,
        execution_timeout_ms=execution_timeout_ms,
        release_timeout_ms=release_timeout_ms,
    )
    try:
        ready = await runtime._receive(None, startup_timeout_ms)
        assert_active(signal)
        required = ("ready", "platform", "backend", "target", "width", "height")
        if ready.get("ready") is not True or any(key not in ready for key in required):
            raise RemotePythonRuntimeError("Remote Python worker did not initialize.")
        runtime.platform = str(ready["platform"])
        runtime.backend = str(ready["backend"])
        runtime.target = str(ready["target"])
        runtime.width = int(ready["width"])
        runtime.height = int(ready["height"])
        return runtime
    except BaseException:
        await runtime.close()
        raise
