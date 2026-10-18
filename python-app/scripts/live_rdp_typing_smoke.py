"""Opt-in Notepad file round-trip on a disposable Windows Hyper-V guest.

RDP performs all GUI input; PowerShell Direct only prepares empty files and
reads them back. Credentials come from the selected profile or environment.
The caller is responsible for restoring the guest checkpoint after testing.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.backends import load_backend_config
from app.backends.config import RdpConfig
from app.guest.windows import run_command
from app.profiles import load_profile
from app.remote.runtime import launch_remote_python_runtime

_GUEST_REQUEST = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$credential = [System.Management.Automation.PSCredential]::new(
    $request.username, (ConvertTo-SecureString -String $request.password -AsPlainText -Force))
$result = Invoke-Command -VMName $request.vm -Credential $credential -ScriptBlock {
    param($action, $path)
    switch ($action) {
        'prepare' { [IO.File]::WriteAllText($path, '', [Text.UTF8Encoding]::new($false)); return '' }
        'read' {
            $text = [IO.File]::ReadAllText($path).Replace("`r`n", "`n")
            return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($text))
        }
        'version' { return ((Get-AppxPackage -Name Microsoft.WindowsNotepad).Version -join ',') }
        default { throw 'Unsupported test action' }
    }
} -ArgumentList $request.action, $request.path
@{result=$result; addresses=@((Get-VMNetworkAdapter -VMName $request.vm).IPAddresses)} |
    ConvertTo-Json -Compress -Depth 4
"""


def guest_request(config: RdpConfig, vm_name: str, action: str, path: str = "") -> dict[str, Any]:
    # Keep credentials out of argv, logs and files. This test-only child reads
    # them from stdin, independently of the model worker's input protocol.
    shell = shutil.which("pwsh") or "powershell.exe"
    shell_env = os.environ.copy()
    if shell == "powershell.exe":
        # Windows PowerShell cannot import PowerShell 7 modules from an
        # inherited PSModulePath. Let it discover its own system modules.
        shell_env.pop("PSModulePath", None)
    result = subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _GUEST_REQUEST],
        input=json.dumps({"vm": vm_name,
                          "username": f"{config.domain}\\{config.username}" if config.domain else config.username,
                          "password": config.password,
                          "action": action, "path": path}),
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=shell_env, check=False, timeout=30,
    )
    if result.returncode:
        diagnostic = result.stderr.strip()
        if config.password:
            diagnostic = diagnostic.replace(config.password, "[redacted]")
        raise RuntimeError(f"PowerShell Direct read-back failed: {diagnostic}")
    return json.loads(result.stdout)


async def main(vm_name: str, profile: str | None) -> None:
    if os.name != "nt":
        raise RuntimeError("This opt-in smoke requires a Windows Hyper-V host")
    config = load_profile(profile) if profile is not None else load_backend_config()
    if not isinstance(config, RdpConfig):
        raise TypeError("The Notepad smoke requires an RDP connection")
    guest = await asyncio.to_thread(guest_request, config, vm_name, "version")
    targets = {item[4][0] for item in socket.getaddrinfo(config.host, config.port, type=socket.SOCK_STREAM)}
    if not targets or not targets.issubset(guest["addresses"]):
        raise ValueError("The selected RDP target is not the specified Hyper-V guest")

    output = Path(__file__).resolve().parents[2] / "test-artifacts"
    output.mkdir(exist_ok=True)
    line = "RDP-0123456789 ABCDEFGHIJKLMNOPQRSTUVWXYZ abcdefghijklmnopqrstuvwxyz 你好世界 中文输入 😀"
    cases = [(f"default-{index}", line + "\n" + line, 0.0, False) for index in range(1, 4)]
    cases += [("explicit-interval", "Caller interval: 中文😀\nsecond line", 0.02, False),
              ("atomic", "Atomic: hello\t你好😀\nsecond line", 0.0, True)]
    report: dict[str, Any] = {"notepad_version": guest["result"], "cases": [], "passed": False}
    runtime = None
    verified = False
    try:
        runtime = await launch_remote_python_runtime(config, execution_timeout_ms=90_000)
        for index, (name, text, interval, atomic) in enumerate(cases):
            # A new file avoids Notepad's selection retaining an existing final
            # paragraph. Never strip trailing text when judging the result.
            path = rf"C:\Users\Public\rt-{uuid4().hex[:8]}-{index}.txt"
            await asyncio.to_thread(guest_request, config, vm_name, "prepare", path)
            await run_command(runtime, f'notepad.exe "{path}"')
            write = (f"computer.run_actions([{{'type': 'type', 'text': {text!r}}}])" if atomic
                     else f"computer.write({text!r}, interval={interval!r})")
            result = await runtime.execute(
                "import time\n" + write + "\ntime.sleep(0.5)\ncomputer.hotkey('ctrl','s')\n"
                "time.sleep(1)\nlog('typing_completed')"
            )
            texts = [item["text"] for item in result["output"] if item["type"] == "input_text"]
            if texts != ["typing_completed"]:
                raise RuntimeError(f"{name}: runtime did not complete: {texts}")
            saved = await asyncio.to_thread(guest_request, config, vm_name, "read", path)
            actual = base64.b64decode(saved["result"], validate=True).decode("utf-8")
            matched = actual == text
            report["cases"].append({"name": name, "matched": matched,
                                    "expected_characters": len(text), "actual_characters": len(actual)})
            print(json.dumps(report["cases"][-1]), flush=True)
            if not matched:
                raise AssertionError(f"{name}: saved guest text differs from the complete input")
        (output / "rdp-notepad-typing.png").write_bytes(await runtime.capture_screenshot())
        verified = True
    finally:
        try:
            if runtime is not None:
                await runtime.close()
            report["passed"] = verified
        finally:
            (output / "rdp-notepad-typing.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm-name", required=True, help="Disposable Hyper-V guest to verify and read back")
    parser.add_argument("--profile", help="Saved RDP profile; otherwise use REMOTE_COMPUTER_* settings")
    args = parser.parse_args()
    asyncio.run(main(args.vm_name, args.profile))
