# Remote Computer Use for Codex

English | [简体中文](README_zh.md)

Remote Computer Use is a fork of OpenAI's Computer Use sample applications that is being rebuilt as an MIT-licensed Codex plugin for controlling isolated computers over remote-desktop protocols.

The Python sample's local desktop transports are replaced with RDP and VNC. Its defining agent execution model is retained: the model writes Python, that code runs in one persistent run-scoped process, and the process keeps variables and helpers alive across `exec_py` calls.

A single code call can take several screenshots, perform multiple actions, inspect pixels, and loop until a condition changes. This reduces model round-trips without giving the worker access to the host pointer or keyboard. The retained Responses loop, Console, Replay, Labs, contracts, cancellation, and recovery lifecycle now sit above a remote-computer abstraction instead of a local desktop implementation.

## Architecture

The main path remains the upstream-style code runtime:

```text
Codex Host environment -> bundled MCP server -> named persistent Python worker -> computer -> protocol adapter -> guest GUI
```

One plugin-server instance owns one registry of named remote sessions. When the host provides an isolated server process per conversation, those sessions are naturally scoped to that conversation. This project does not assume that stronger lifecycle rule from MCP itself: OpenAI's public documentation defines a stdio MCP server as [a process in the session environment](https://developers.openai.com/api/docs/guides/agents-api/tools/mcp). Each registered name owns its persistent worker, operation lock, and static connection metadata, so code for different hosts can run concurrently without sharing Python globals or input state.

RDP and VNC are independent, first-class backends behind `computer`. Each backend delegates library-specific behavior through a narrow protocol adapter; `ComputerSession` and the persistent runtime do not depend on aardwolf or vnc-remote-control APIs.

| Backend | Purpose | Implementation boundary |
| --- | --- | --- |
| RDP | Windows and other RDP targets | `RDPBackend` -> `RDPAdapter` -> pinned `AardwolfAdapter` |
| VNC/RFB | RFB 3.8 servers with Raw encoding and None or VNC-password authentication | `VNCBackend` -> `VNCAdapter` -> pinned `VncRemoteControlAdapter` |

The repository root is the Codex plugin root and includes its Python MCP runtime. The runtime's preferred tool, `computer_exec_py`, uses the same persistent worker as the retained Responses loop. Its globals are `computer`, `pyautogui`, `Image`, `log`, and `display`; ordinary Python globals survive between calls. The atomic MCP tools remain as a convenience surface and route through that same worker-owned connection.

## Install

Install Codex, `uv`, and Python 3.12, with `uv` available on `PATH`. Open the [project repository](https://github.com/Napreth/RemoteComputerUse-for-Codex), choose **Code → Download ZIP**, and keep the downloaded archive. In the Codex client, click **Customize** in the sidebar, open **Plugins**, click **Add**, then choose **Upload plugin ZIP** and select the archive. Enter Plugins through the sidebar: the Plugins page reached from Settings may not show the upload option. Developers working from a checkout can run `uv sync --project python-app --frozen` at the repository root.

The root [plugin manifest](plugin.json) discovers `skills/` and the bundled `remote_computer` server in [mcp.json](mcp.json). Codex starts `uv run --frozen --no-dev --project ${PLUGIN_ROOT}/python-app remote-computer-use`, expanding `${PLUGIN_ROOT}` to the installed plugin root and using that project's lockfile. Users do not need to locate the installation or supply a source path. The runtime does not launch a local browser or control the host desktop.

The first start may need network access to install the locked Python dependencies. The [Codex main implementation](https://github.com/openai/codex/blob/main/codex-rs/codex-mcp/src/rmcp_client.rs) checked on 2026-10-15 gives MCP startup 30 seconds and tool calls 300 seconds. Agent Plugins 1.0.0 `mcp.json` has no timeout fields, and the documented plugin user policy provides no timeout override. **1.0 release is blocked:** the clean Windows cold-cache check exceeded 30 seconds before initialization. See [release compatibility](docs/release-compatibility.md). Cached dependency installation alone does not establish first-install compatibility.

## Configure

Create a user-owned `config.json` at `%LOCALAPPDATA%\remote-computer-use-for-codex\config.json` on Windows or `${XDG_CONFIG_HOME:-$HOME/.config}/remote-computer-use-for-codex/config.json` on Linux. An unset or relative `XDG_CONFIG_HOME` uses `$HOME/.config`. If Codex omits `LOCALAPPDATA`, the runtime queries the current user's Windows folder directly. The plugin does not create this file. Use integer schema version `1`, unique profile names, and one password source per profile:

```json
{
  "version": 1,
  "profiles": [
    {"name": "vm-dev", "type": "rdp", "host": "192.0.2.10", "username": "alice", "password": "<your-password>"},
    {"name": "vm-test", "type": "vnc", "host": "192.0.2.11", "port": 5900, "passwdenv": "VM_TEST_PASSWORD"}
  ]
}
```

Call `computer_list_profiles()` to obtain only profile names and backend types, then `computer_connect(profile="vm-dev", name="work")`. The profile selects saved settings; `name` selects the running session. Profile connections reject all explicit connection overrides, so saved credentials cannot be redirected to a different target through tool parameters. Each connection rereads and validates the file; edits affect new connections, while existing sessions and emergency cleanup retain their original resolved settings. Malformed JSON, duplicate names/properties, unknown fields, and conflicting `password`/`passwdenv` are rejected without echoing file contents. The file size limit is 1 MiB.

RDP profiles require `host` and `username`; optional fields are `port` (3389), `domain` (empty), `width` (1440), `height` (900), and `insecure_tls` (false). VNC profiles require `host`; optional fields are `port` (5900) and `shared` (true only). Exclusive VNC sessions (`shared=false`) are unsupported and rejected during profile validation. `password` is a string and defaults to empty when neither credential field is provided. `passwdenv` names an existing MCP-process variable using letters, digits, and underscores, starting with a letter or underscore; an absent variable is an error. It does not request Host forwarding or load `.env`. Referenced credential variables are removed from worker environments before launch; the parent retains the resolved password for cleanup.

Hosts may be DNS hostnames, ASCII internal names such as `rdp_service` or `win_host.local`, IPv4 literals, or IPv6 literals; IPv6 may be bracketed in the configuration. Internationalized DNS names use IDNA2008 with UTS #46 nontransitional processing, so `faß.de` becomes `xn--fa-hia.de`, not `fass.de`. Names are lowercased, and single-label machine names are supported. Underscores are allowed only in entirely ASCII internal names; each label must contain 1–63 letters, digits, underscores, or hyphens, with no hyphen at either end. Supply the port separately. URLs, userinfo, paths, query strings, fragments, percent escapes, IPv6 scope identifiers, and ambiguous nonstandard numeric IPv4 forms are rejected. The RDP URL builder also validates host and port for directly constructed configurations.

If `config.json` is absent, `computer_list_profiles()` returns an empty list without creating it. Existing environment-based users can then call `computer_connect(name="work")` without `profile`. An explicit profile request still reports a missing file, and malformed or unreadable files remain discovery errors; they are not silently ignored.

Plaintext passwords in this file are readable by `computer_exec_py`, which runs under the same OS account. Restrict file permissions to the intended user (for example, `0600` on Linux and user-only ACLs on Windows), keep it outside the repository and plugin ZIP, and do not treat those permissions as isolation from same-account Python. The plugin has no credential-writing tools, and passwords are never accepted as connection tool arguments.

Optional `UV_*` values are interpreted by uv exactly as supplied by the Host. The plugin supplies neither uv defaults nor an environment-variable forwarding list, and contains no credential values.

Explicit connection arguments inherit matching environment settings only when omitted or `None`. Empty `host` or `backend` values and an empty RDP `username` are rejected; `domain=""` explicitly clears an environment-provided RDP domain. Ports and dimensions must be integers within their supported ranges; zero, booleans, and floats are rejected rather than replaced with environment values or defaults.

Legacy explicit `computer_connect(backend=..., host=...)` calls remain supported and read matching `REMOTE_COMPUTER_*` settings from the MCP environment. The [bundled MCP user-environment gap](https://github.com/openai/codex/issues/36854) remains upstream: use a plaintext profile password when the Host cannot inject a referenced variable. Profiles provide a connection-configuration path without that Host feature; they do not configure uv or add an environment forwarding list. With no target configuration, MCP initialization and tool discovery still work, while `computer_connect` reports a configuration error. The following environment examples apply only when the Host already supplies those values:

RDP:

```text
REMOTE_COMPUTER_BACKEND=rdp
REMOTE_COMPUTER_RDP_HOST=192.0.2.10
REMOTE_COMPUTER_RDP_USERNAME=codex
REMOTE_COMPUTER_RDP_PASSWORD=...
```

VNC:

```text
REMOTE_COMPUTER_BACKEND=vnc
REMOTE_COMPUTER_VNC_HOST=192.0.2.11
REMOTE_COMPUTER_VNC_PASSWORD=...
```

Optional RDP variables are `REMOTE_COMPUTER_RDP_PORT`, `REMOTE_COMPUTER_RDP_DOMAIN`, `REMOTE_COMPUTER_RDP_WIDTH`, `REMOTE_COMPUTER_RDP_HEIGHT`, and `REMOTE_COMPUTER_RDP_INSECURE_TLS`. Optional VNC variables are `REMOTE_COMPUTER_VNC_PORT` and `REMOTE_COMPUTER_VNC_SHARED`.

Retained Labs support Windows guests with a working Win+R shortcut and Edge. VNC navigation requires explicit `REMOTE_COMPUTER_VNC_WINDOWS_NAVIGATION=win-r` after verifying the server forwards that shortcut. Other VNC Windows layouts and shortcut mappings are not supported by automatic navigation; no fixed taskbar coordinates are used. Set `REMOTE_COMPUTER_GUEST_OS=windows`; the runner then serves only the selected mutable lab workspace on a guest-reachable host interface, explicitly opens its URL in the guest, and waits for the guest's request before starting the model loop. Route detection is automatic. For unusual networking, override `REMOTE_COMPUTER_LAB_HOST` (the address placed in the guest URL), `REMOTE_COMPUTER_LAB_BIND_HOST` (the local interface to bind), and optionally `REMOTE_COMPUTER_LAB_PORT` (`0` selects an ephemeral port). The chosen interface and port must be reachable through the host firewall from the guest.

Each lab server generates a fresh 128-bit capability path. Its HTML and static assets are available only below that path, and guest runs additionally accept only the resolved RDP/VNC target IP addresses as HTTP peers. When forwarding or NAT changes the guest's HTTP source address, set `REMOTE_COMPUTER_LAB_GUEST_HOST` to that address or hostname. Forwarded HTTP headers do not override this check. Readiness requires a successful `GET` of that run's index from an allowed peer; it confirms delivery to the guest, not completion of JavaScript rendering. URLs contain access tokens and should not be shared while a run is active.

## Quick start

After uploading the plugin ZIP and creating the user profiles file, start a new Codex session, check that the bundled `remote_computer` tools are available, connect to a target, inspect its framebuffer, run Python against the same remote computer, then disconnect:

```text
computer_list_profiles()
computer_connect(profile="vm-dev", name="win-dev")
computer_screenshot(session="win-dev")
computer_exec_py(session="win-dev", code="display(computer.screenshot())")
computer_disconnect(session="win-dev")
```

The RDP or VNC password comes from the selected profile or existing environment. Inside `computer_exec_py`, use `computer` for remote input and screenshots; `pyautogui` and `import pyautogui` resolve to the same remote compatibility facade. Ordinary Python globals remain available in later calls to the same session. For a single connected computer, later tool calls may omit `session`; name it explicitly when using multiple computers. See the [Chinese guide](README_zh.md) for a complete walkthrough.

## Tools

Codex receives one protocol-neutral surface: persistent Python execution, status, connect, screenshot, pointer move/click/drag/scroll, Unicode text, key chords, bounded action batches, and disconnect. Selecting a backend creates that backend directly; neither backend is routed through the other.

Connect one or more hosts with stable names, then prefer `computer_exec_py`:

```text
computer_connect(name="win-dev", backend="rdp", host="192.0.2.10", ...)
computer_connect(name="linux-test", backend="vnc", host="192.0.2.11", ...)
computer_exec_py(session="win-dev", code="...")
computer_screenshot(session="linux-test")
```

The first connection may omit `name` and then receives the name `default`. After any session is registered, every further `computer_connect` call must provide `name`; an unnamed connect never silently replaces the existing host. For operations, `session` may be omitted when exactly one registered runtime is still connected and is rejected when multiple connected hosts would be ambiguous.

`computer_list` returns every registered entry immediately from static metadata, including its `connected` and `busy` state. It does not wait for a long-running operation on another host. A disconnected registry entry is not counted when resolving an omitted operation `session`; explicitly disconnecting an entry removes it and its lock.

During disconnect, the name and its lock stay registered until worker shutdown and emergency input release finish. Same-name reconnects wait for that cleanup, including after a tool caller is cancelled. A failed cleanup leaves the name blocked; recover the remote input state and restart the plugin server before reusing it. Other named computers remain independently available.

Inside a selected persistent worker, for example:

```python
frame = computer.screenshot()
display(frame)
for x in (420, 560, 700):
    computer.click(x, 300)
display(computer.screenshot())
```

The worker also exposes `computer.run_actions([...])` for validated bounded batches. `computer.screenshot()` returns a Pillow image in the same coordinate space used by input methods.

`computer_status` reflects last-known backend transport health rather than merely the presence of a Python object. VNC health is passive: status never reads the socket, while any failed VNC I/O latches the backend as disconnected. Batched actions use a discriminated schema with extra fields forbidden for move, click, drag, scroll, type, keypress, and wait.

Atomic tools, action batches, and Python calls share the last successfully sent pointer position. `computer.position()` returns that position; `computer.click()` defaults to it, and `computer.drag_to()` uses it as the start of the drag. This is not a query of pointer movement caused by another remote client or user. An invalid drag path is rejected before any mouse button is pressed. After a batch fails partway through, the position reflects the last successful action. Default-coordinate actions resolve that position and perform their input under one session lock, including when persistent Python code uses multiple threads.

The `pyautogui` global and `import pyautogui` use a remote compatibility facade; they never import the host input library. Both it and `computer` support common PyAutoGUI movement, click, sequential press, hotkey, key/button hold, typing, and screenshot calls. `scroll(10)` scrolls up ten wheel clicks; `scroll(-10)` scrolls down ten. Atomic `computer_scroll` and `run_actions` retain their explicit protocol deltas (positive down/right, 120 per wheel click). The full supported surface and remaining remote limitations are recorded in [the compatibility contract](docs/upstream-compatibility.md).

Worker replies include last-known transport health even when Python catches an exception. A detected disconnect during code or input execution closes the worker and attempts bounded emergency input release. In the retained runner, a failed release is reported as a failed run and blocks new runs until recovery and restart.

VNC supports horizontal and vertical wheel events. RDP supports vertical wheel events; requesting a horizontal RDP wheel event returns an explicit capability error instead of silently doing the wrong thing.

## Security model

- **`computer_exec_py` is trusted host code execution, not a sandbox.** The persistent child process executes model-generated Python with complete builtins under the same OS account as the MCP server. It can read and write files available to that account, import modules, start processes, and use the network. Remote-desktop isolation prevents takeover of the host pointer and keyboard; it does not isolate Python from the host OS. Run the plugin under a dedicated least-privileged account, container, or VM when the generated code or prompt content is not fully trusted.
- Credentials are supplied by user profiles or the existing server environment and must not be committed. Plaintext profile passwords are accessible to same-account Python; this is not a secret store or sandbox boundary.
- Remote backend password variables are removed from the persistent worker's environment after its connection starts, but that hygiene is not an OS security boundary.
- Remote screenshots and input travel through the selected remote protocol; the host computer's pointer and keyboard are not controlled.
- Mutating tools remain approval-gated by Codex.
- VNC deployments should prefer a protected network or an authenticated tunnel.
- Traditional VNC password authentication does not encrypt the framebuffer or input stream. The current VNC backend does not implement VeNCrypt/TLS.
- The VNC compatibility boundary is deliberately narrow: RFB 3.8, Raw framebuffer encoding, and None/VNC-password authentication. Tight, ZRLE, proprietary extensions, and enterprise authentication are not advertised.
- The pinned `vnc-remote-control==1.0.4` adapter lacks public raw key/pointer event methods, so this MVP wraps its version-pinned private event primitives.
- The pinned `aardwolf==0.2.13` adapter contains the Unicode compatibility shim and private transport hard-close fallback. Those internals do not escape the adapter package.
- VNC connection setup has both per-socket timeouts and a total handshake deadline; deadline expiry closes the socket and consumes the worker result.

## Development

To build an upload ZIP, run `python scripts/package_plugin.py` (Python 3.12+, standard library only), or `pnpm plugin:pack`. The default output is `dist/remote-computer-use-1.0.0.zip`, named from `plugin.json`; use `--output <path>.zip` to choose a destination. The script locates the checkout from its own path, so it also works from another current directory and from a downloaded source ZIP without Git. It stages only manifests, the MCP runtime, its locked Python project, the skill, license and user documentation, excluding local configuration, virtual environments, caches, tests, Console and Labs. Entry order, timestamps and permissions are fixed for repeatable archives; source links are rejected. Upload the generated ZIP through **Customize → Plugins → Add → Upload plugin ZIP**. Packaging does not validate the deferred release compatibility requirements.

Development happens directly on `main`. Repository commits are intentionally left unsigned during implementation so the maintainer can sign the finished history later.

Run the plugin checks with:

```powershell
pnpm check
```

The check command covers Python, Console/contracts, shared lab unit tests and the Console production build. `pnpm dev:python` launches the runner and Console with the caller's process environment; `pnpm start:python` uses the built Console. Neither launcher reads a repository `.env` file. The incomplete JavaScript/Playwright backend is retired and excluded from the active workspace. Its source and old JavaScript lab integration tests remain historical references, not supported build targets. No local desktop backend is restored by the compatibility layer.

For the complete retained Python framework suite, run `uv run --frozen --project python-app pytest -s -p no:cacheprovider python-app/tests`.

The inherited run lifecycle, cancellation, screenshot/replay storage, and Responses code loop use the same persistent remote worker. Execution deadlines terminate the worker process; if an operation is interrupted while input may be held, a bounded reconnect-and-release helper runs before the session is allowed to finish. Request-scoped validation/capability failures from atomic tools are reported without discarding that worker's Python globals or connection. Guest-specific workflows such as opening a URL through the Windows Run dialog remain outside the protocol-neutral runtime. The deterministic `python-app/scripts/live_remote_lab_smoke.py` check verifies that the configured guest requests the selected lab, then captures a subsequent framebuffer without invoking a model. It records the image dimensions and saves the screenshot for manual inspection; it does not assert that the selected lab rendered in the image. Setting `REMOTE_COMPUTER_LAB_SMOKE_URL` also skips the local lab server's request acknowledgement.

The persistent-runtime smoke requires the same guest-reachable HTTP settings as Labs. It rejects Python error output and requires terminal markers and final images. Success also requires the guest to write and read back its random marker, then request a capability-protected URL from the expected guest IP. A missing callback fails the smoke even if earlier logs or screenshots were produced.

## License

MIT License, Copyright (c) 2026 Napreth. The license inherited from the OpenAI sample repository is preserved in [`LICENSES/OpenAI-CUA-Sample-App-LICENSE`](LICENSES/OpenAI-CUA-Sample-App-LICENSE).

The original sample README is retained at [`docs/upstream/openai-sample-README.md`](docs/upstream/openai-sample-README.md) for architectural reference.
