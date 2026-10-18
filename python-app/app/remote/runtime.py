"""Own the persistent remote Python worker and its cancellation boundaries."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from ..backends import BackendConfig, RdpConfig, VncConfig
from ..input_tracking import validate_identities
from ..processes import (
    MAX_PROCESS_OUTPUT_BYTES,
    OwnedProcess,
    assert_active,
    await_active,
    child_environment,
    drain_tail,
)

logger = logging.getLogger(__name__)


class RemotePythonRuntimeError(RuntimeError):
    def __init__(self, error: Any, code: str = "python_operation_failed") -> None:
        if isinstance(error, dict):
            code = error.get("code", code)
            error = error.get("message", str(error))
        super().__init__(str(error))
        self.code = code


class RemotePythonOperationError(RemotePythonRuntimeError):
    """A request failed without invalidating the persistent worker."""


class RemotePythonStartupCleanupError(RemotePythonRuntimeError):
    """Startup failed and its worker could not be safely cleaned up."""


async def _complete_startup_cleanup(cleanup: Coroutine[Any, Any, None]) -> None:
    """Defer repeated launch cancellation until worker ownership is resolved."""
    task = asyncio.create_task(cleanup)
    try:
        while True:
            try:
                await asyncio.shield(task)
                return
            except asyncio.CancelledError:
                if task.cancelled():
                    raise RemotePythonStartupCleanupError(
                        "Remote Python startup cleanup was cancelled before completion.",
                        "startup_cleanup_failed",
                    ) from None
                # The launch caller was cancelled again. The cleanup task still
                # owns the worker; wait for its result before propagating failure.
    except RemotePythonStartupCleanupError:
        raise
    except Exception as error:
        raise RemotePythonStartupCleanupError(
            f"Remote Python startup cleanup failed: {error}", "startup_cleanup_failed"
        ) from error


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
        self._transport_connected = True
        self._closing: asyncio.Task[None] | None = None
        self._pending_identities: set[int] = set()
        self._identity_error: Exception | None = None
        self._identity_generation = 0
        self._received_identity_generation = 0
        self._responses: asyncio.Queue[tuple[bytes | Exception, int]] = asyncio.Queue()
        self._stdout = asyncio.create_task(self._read_worker_output())
        self._stderr = asyncio.create_task(drain_tail(child.stderr))
        self._watcher = asyncio.create_task(self._watch())

    async def _read_worker_output(self) -> None:
        try:
            while True:
                line = await self.child.stdout.readline()
                if not line:
                    self._responses.put_nowait((b"", self._identity_generation))
                    return
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError) as error:
                    self._identity_error = error
                    self._responses.put_nowait((line, self._identity_generation))
                    continue
                if isinstance(value, dict) and value.get("kind") == "input_identity":
                    self._input_may_be_held = True
                    self._identity_generation += 1
                    try:
                        protocol = self.environment["REMOTE_COMPUTER_BACKEND"]
                        if value.get("protocol") != protocol:
                            raise ValueError("input identity protocol mismatch")
                        self._pending_identities.update(validate_identities(protocol, value.get("identities")))
                    except (ValueError, TypeError, KeyError) as error:
                        self._identity_error = error
                    continue
                self._responses.put_nowait((line, self._identity_generation))
        except Exception as error:  # noqa: BLE001 - no uncertain input can be reported released
            # Truncated identity frames must never produce a successful sweep.
            self._identity_error = error
            self._responses.put_nowait((error, self._identity_generation))

    async def _next_response(self) -> bytes:
        value, self._received_identity_generation = await self._responses.get()
        if self._identity_error is not None:
            raise RemotePythonRuntimeError("Input identity stream failed.") from self._identity_error
        if isinstance(value, Exception):
            raise value
        return value

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
        line_task = asyncio.create_task(self._next_response())
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
            if len(line) > MAX_PROCESS_OUTPUT_BYTES:
                raise RemotePythonRuntimeError("Python output exceeds 12 MiB.")
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise TypeError("Invalid response.")
                if expected_id is not None and value.get("id") != expected_id:
                    if "id" not in value and "error" in value:
                        raise RemotePythonRuntimeError(value["error"])
                    raise ValueError(
                        f"Unexpected response ID {value.get('id')!r}; expected {expected_id!r}."
                    )
                if expected_id is None and "id" in value:
                    raise ValueError("Unexpected startup response ID.")
            except (TypeError, ValueError, UnicodeError) as error:
                raise RemotePythonRuntimeError(
                    f"Python worker returned invalid JSON or response ID: {error}"
                ) from error
            if isinstance(value.get("connected"), bool):
                self._transport_connected = value["connected"]
            if isinstance(value.get("inputHeld"), bool):
                self._reported_input_held = value["inputHeld"]
            if "error" in value:
                response_error = value["error"]
                if (
                    isinstance(response_error, dict)
                    and response_error.get("recoverable") is True
                    and self._transport_connected
                ):
                    raise RemotePythonOperationError(response_error)
                raise RemotePythonRuntimeError(response_error)
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
            if not self._transport_connected and self._input_may_be_held:
                # A swallowed Python exception can leave input held on a dead
                # transport. Close and run emergency release before clearing it.
                await self.close()
            self._input_may_be_held = (self._reported_input_held
                                       or self._identity_generation > self._received_identity_generation)
            return value
        except RemotePythonOperationError:
            self._input_may_be_held = (self._reported_input_held
                                       or self._identity_generation > self._received_identity_generation)
            raise
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
        self._transport_connected = data.get("connected") is True
        return data

    @property
    def connected(self) -> bool:
        return self._transport_connected and self._closing is None and self.child.process.returncode is None

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
                payload = json.dumps({"protocol": self.environment["REMOTE_COMPUTER_BACKEND"],
                                      "identities": sorted(self._pending_identities)}) + "\n"
                async def deliver_and_wait() -> None:
                    await helper.write(payload.encode())
                    await helper.wait()
                await asyncio.wait_for(deliver_and_wait(), self.release_timeout_ms / 1000)
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
        if self._identity_error is not None:
            raise RemotePythonRuntimeError(
                "Input identities were incomplete; known inputs were released but full release cannot be confirmed.",
                "input_release_failed",
            ) from self._identity_error

    async def _close(self) -> None:
        failure: Exception | None = None
        try:
            await self.child.close()
        except Exception as error:  # noqa: BLE001 - release must still be attempted
            failure = error
        finally:
            await self._stderr
            await self._stdout
        if self._input_may_be_held or self._identity_error is not None:
            await self._release_inputs()
        if failure is not None:
            raise failure
        self._pending_identities.clear()

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
    secret_names = {name.upper() if sys.platform == "win32" else name for name in config.secret_env_names}
    for name in tuple(environment):
        if (name.upper() if sys.platform == "win32" else name) in secret_names:
            environment.pop(name)
    environment.update(_config_environment(config))
    spawning = asyncio.create_task(
        OwnedProcess.spawn(
            [python_path, "-u", str(worker_path or directory / "worker.py")],
            env=environment,
        )
    )
    try:
        child = await asyncio.shield(spawning)
    except asyncio.CancelledError:
        async def reap_spawn() -> None:
            try:
                spawned = await spawning
            except Exception:  # noqa: BLE001 - spawn failed without returning a child
                return
            await spawned.close()

        # Spawn may have created an OS process before yielding its handle. Keep
        # that operation alive and reap its result before declaring cancellation.
        await _complete_startup_cleanup(reap_spawn())
        raise
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
        await _complete_startup_cleanup(runtime.close())
        raise
