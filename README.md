# Remote Computer Use for Codex

Remote Computer Use is a fork of OpenAI's Computer Use sample applications that
is being rebuilt as an MIT-licensed Codex plugin for controlling isolated
computers over remote-desktop protocols.

The Python sample's local desktop transports are replaced with RDP and VNC.
Its defining agent execution model is retained: the model writes Python,
that code runs in one persistent run-scoped process, and the process keeps
variables and helpers alive across `exec_py` calls.

A single code call can take several screenshots, perform multiple actions,
inspect pixels, and loop until a condition changes. This reduces model
round-trips without giving the worker access to the host pointer or keyboard.
The retained Responses loop, Console, Replay, Labs, contracts, cancellation,
and recovery lifecycle now sit above a remote-computer abstraction instead of
a local desktop implementation.

## Architecture

The main path remains the upstream-style code runtime:

```text
host/session environment -> plugin-server instance -> named persistent Python worker -> computer -> protocol adapter -> guest GUI
```

One plugin-server instance owns one registry of named remote sessions. When the
host provides an isolated server process per conversation, those sessions are
naturally scoped to that conversation. This project does not assume that
stronger lifecycle rule from MCP itself: OpenAI's public documentation defines
a stdio MCP server as [a process in the session
environment](https://developers.openai.com/api/docs/guides/agents-api/tools/mcp).
Each registered name owns its persistent worker, operation lock, and static
connection metadata, so code for different hosts can run concurrently without
sharing Python globals or input state.

RDP and VNC are independent, first-class backends behind `computer`. Each
backend delegates library-specific behavior through a narrow protocol adapter;
`ComputerSession` and the persistent runtime do not depend on aardwolf or
vnc-remote-control APIs.

| Backend | Purpose | Implementation boundary |
| --- | --- | --- |
| RDP | Windows and other RDP targets | `RDPBackend` -> `RDPAdapter` -> pinned `AardwolfAdapter` |
| VNC/RFB | RFB 3.8 servers with Raw encoding and None or VNC-password authentication | `VNCBackend` -> `VNCAdapter` -> pinned `VncRemoteControlAdapter` |

The repository root is the Codex plugin root. The plugin's preferred MCP tool,
`computer_exec_py`, uses the same persistent worker as the retained Responses
loop. Its globals are `computer`, `pyautogui`, `Image`, `log`, and `display`; ordinary Python
globals survive between calls. The atomic MCP tools remain as a convenience
surface and route through that same worker-owned connection.

## Install

Prerequisites are Codex, `uv`, and Python 3.12. From a checkout of this
repository, install the plugin with Codex's local-plugin workflow. During
development, run:

```powershell
uv sync --project python-app --frozen
```

The plugin starts `remote-computer-use` from [`.mcp.json`](.mcp.json). It does
not launch a local browser or control the host desktop.

## Configure

Set `REMOTE_COMPUTER_BACKEND` to `rdp` or `vnc`, then provide the matching
variables to Codex's MCP server environment. Credentials are read only by the
server process and are deliberately absent from tool arguments.

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

Optional RDP variables are `REMOTE_COMPUTER_RDP_PORT`,
`REMOTE_COMPUTER_RDP_DOMAIN`, `REMOTE_COMPUTER_RDP_WIDTH`,
`REMOTE_COMPUTER_RDP_HEIGHT`, and `REMOTE_COMPUTER_RDP_INSECURE_TLS`. Optional
VNC variables are `REMOTE_COMPUTER_VNC_PORT` and
`REMOTE_COMPUTER_VNC_SHARED`.

Retained Labs support Windows guests with a working Win+R shortcut and Edge.
VNC navigation requires explicit `REMOTE_COMPUTER_VNC_WINDOWS_NAVIGATION=win-r`
after verifying the server forwards that shortcut. Other VNC Windows layouts
and shortcut mappings are not supported by automatic navigation; no fixed
taskbar coordinates are used. Set
`REMOTE_COMPUTER_GUEST_OS=windows`; the runner then serves only the selected
mutable lab workspace on a guest-reachable host interface, explicitly opens its
URL in the guest, and waits for the guest's request before starting the model
loop. Route detection is automatic. For unusual networking, override
`REMOTE_COMPUTER_LAB_HOST` (the address placed in the guest URL),
`REMOTE_COMPUTER_LAB_BIND_HOST` (the local interface to bind), and optionally
`REMOTE_COMPUTER_LAB_PORT` (`0` selects an ephemeral port). The chosen interface
and port must be reachable through the host firewall from the guest.

Each lab server generates a fresh 128-bit capability path. Its HTML and static
assets are available only below that path, and guest runs additionally accept
only the resolved RDP/VNC target IP addresses as HTTP peers. When forwarding or
NAT changes the guest's HTTP source address, set `REMOTE_COMPUTER_LAB_GUEST_HOST`
to that address or hostname. Forwarded HTTP headers do not override this check.
Readiness requires a successful `GET` of that run's index from an allowed peer;
it confirms delivery to the guest, not completion of JavaScript rendering. URLs
contain access tokens and should not be shared while a run is active.

## Tools

Codex receives one protocol-neutral surface: persistent Python execution,
status, connect, screenshot, pointer move/click/drag/scroll, Unicode text, key
chords, bounded action batches, and disconnect. Selecting a backend creates
that backend directly; neither backend is routed through the other.

Connect one or more hosts with stable names, then prefer `computer_exec_py`:

```text
computer_connect(name="win-dev", backend="rdp", host="192.0.2.10", ...)
computer_connect(name="linux-test", backend="vnc", host="192.0.2.11", ...)
computer_exec_py(session="win-dev", code="...")
computer_screenshot(session="linux-test")
```

The first connection may omit `name` and then receives the name `default`.
After any session is registered, every further `computer_connect` call must
provide `name`; an unnamed connect never silently replaces the existing host.
For operations, `session` may be omitted when exactly one registered runtime is
still connected and is rejected when multiple connected hosts would be
ambiguous.

`computer_list` returns every registered entry immediately from static
metadata, including its `connected` and `busy` state. It does not wait for a
long-running operation on another host. A disconnected registry entry is not
counted when resolving an omitted operation `session`; explicitly disconnecting
an entry removes it and its lock.

During disconnect, the name and its lock stay registered until worker shutdown
and emergency input release finish. Same-name reconnects wait for that cleanup,
including after a tool caller is cancelled. A failed cleanup leaves the name
blocked; recover the remote input state and restart the plugin server before
reusing it. Other named computers remain independently available.

Inside a selected persistent worker, for example:

```python
frame = computer.screenshot()
display(frame)
for x in (420, 560, 700):
    computer.click(x, 300)
display(computer.screenshot())
```

The worker also exposes `computer.run_actions([...])` for validated bounded
batches. `computer.screenshot()` returns a Pillow image in the same coordinate
space used by input methods.

`computer_status` reflects last-known backend transport health rather than
merely the presence of a Python object. VNC health is passive: status never
reads the socket, while any failed VNC I/O latches the backend as disconnected.
Batched actions use a discriminated schema with extra fields forbidden for
move, click, drag, scroll, type, keypress, and wait.

Atomic tools, action batches, and Python calls share the last successfully sent
pointer position. `computer.position()` returns that position; `computer.click()`
defaults to it, and `computer.drag_to()` uses it as the start of the drag.
This is not a query of pointer movement caused by another remote client or user.
An invalid drag path is rejected before any mouse button is pressed. After a
batch fails partway through, the position reflects the last successful action.
Default-coordinate actions resolve that position and perform their input under
one session lock, including when persistent Python code uses multiple threads.

The `pyautogui` global and `import pyautogui` use a remote compatibility facade;
they never import the host input library. Both it and `computer` support common
PyAutoGUI movement, click, sequential press, hotkey, key/button hold, typing, and
screenshot calls. `scroll(10)` scrolls up ten wheel clicks; `scroll(-10)` scrolls
down ten. Atomic `computer_scroll` and `run_actions` retain their explicit
protocol deltas (positive down/right, 120 per wheel click). The full supported
surface and remaining remote limitations are recorded in
[the compatibility contract](docs/upstream-compatibility.md).

Worker replies include last-known transport health even when Python catches an
exception. A detected disconnect during code or input execution closes the worker
and attempts bounded emergency input release. In the retained runner, a failed
release is reported as a failed run and blocks new runs until recovery and restart.

VNC supports horizontal and vertical wheel events. RDP supports vertical wheel
events; requesting a horizontal RDP wheel event returns an explicit capability
error instead of silently doing the wrong thing.

## Security model

- **`computer_exec_py` is trusted host code execution, not a sandbox.** The
  persistent child process executes model-generated Python with complete
  builtins under the same OS account as the MCP server. It can read and write
  files available to that account, import modules, start processes, and use the
  network. Remote-desktop isolation prevents takeover of the host pointer and
  keyboard; it does not isolate Python from the host OS. Run the plugin under a
  dedicated least-privileged account, container, or VM when the generated code
  or prompt content is not fully trusted.
- Credentials are supplied at runtime and must not be committed.
- Remote backend password variables are removed from the persistent worker's
  environment after its connection starts, but that hygiene is not an OS
  security boundary.
- Remote screenshots and input travel through the selected remote protocol;
  the host computer's pointer and keyboard are not controlled.
- Mutating tools remain approval-gated by Codex.
- VNC deployments should prefer a protected network or an authenticated tunnel.
- Traditional VNC password authentication does not encrypt the framebuffer or
  input stream. The current VNC backend does not implement VeNCrypt/TLS.
- The VNC compatibility boundary is deliberately narrow: RFB 3.8, Raw
  framebuffer encoding, and None/VNC-password authentication. Tight, ZRLE,
  proprietary extensions, and enterprise authentication are not advertised.
- The pinned `vnc-remote-control==1.0.4` adapter lacks public raw key/pointer
  event methods, so this MVP wraps its version-pinned private event primitives.
- The pinned `aardwolf==0.2.13` adapter contains the Unicode compatibility shim
  and private transport hard-close fallback. Those internals do not escape the
  adapter package.
- VNC connection setup has both per-socket timeouts and a total handshake
  deadline; deadline expiry closes the socket and consumes the worker result.

## Development

Development happens directly on `main`. Repository commits are intentionally
left unsigned during implementation so the maintainer can sign the finished
history later.

Run the plugin checks with:

```powershell
pnpm check
```

The check command covers Python, Console/contracts, shared lab unit tests and
the Console production build. `pnpm dev:python` launches the runner and Console;
`pnpm start:python` uses the built Console. The incomplete JavaScript/Playwright
backend is retired and excluded from the active workspace. Its source and old
JavaScript lab integration tests remain historical references, not supported
build targets. No local desktop backend is restored by the compatibility layer.

For the complete retained Python framework suite, run
`python-app\.venv\Scripts\python -m pytest -s -p no:cacheprovider
python-app\tests`.

The inherited run lifecycle, cancellation, screenshot/replay storage, and
Responses code loop use the same persistent remote worker. Execution deadlines
terminate the worker process; if an operation is interrupted while input may be
held, a bounded reconnect-and-release helper runs before the session is allowed
to finish. Request-scoped validation/capability failures from atomic tools are
reported without discarding that worker's Python globals or connection.
Guest-specific workflows such as opening a URL through the Windows Run dialog
remain outside the protocol-neutral runtime. The deterministic
`python-app/scripts/live_remote_lab_smoke.py` check verifies that the configured
guest requests the selected lab, then captures a subsequent framebuffer without
invoking a model. It records the image dimensions and saves the screenshot for
manual inspection; it does not assert that the selected lab rendered in the
image. Setting `REMOTE_COMPUTER_LAB_SMOKE_URL` also skips the local lab server's
request acknowledgement.

## License

MIT License, Copyright (c) 2026 Napreth. The license inherited from the OpenAI
sample repository is preserved in
[`LICENSES/OpenAI-CUA-Sample-App-LICENSE`](LICENSES/OpenAI-CUA-Sample-App-LICENSE).

The original sample README is retained at
[`docs/upstream/openai-sample-README.md`](docs/upstream/openai-sample-README.md)
for architectural reference.


The persistent-runtime smoke requires the same guest-reachable HTTP settings as
Labs. It rejects Python error output and requires terminal markers and final
images. Success also requires the guest to write and read back its random marker,
then request a capability-protected URL from the expected guest IP. A missing
callback fails the smoke even if earlier logs or screenshots were produced.
