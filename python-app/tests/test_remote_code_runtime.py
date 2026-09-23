from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.backends.config import RdpConfig
from app.remote.runtime import RemotePythonRuntimeError, launch_remote_python_runtime

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
