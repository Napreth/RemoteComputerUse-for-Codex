# Remote Computer Use for Codex

Remote Computer Use is a fork of OpenAI's Computer Use sample applications that
is being rebuilt as an MIT-licensed Codex plugin for controlling isolated
computers over remote-desktop protocols.

This is a replacement product, not an additional backend for the original
local Computer Use sample. Its Playwright and PyAutoGUI transports are removed,
while its defining agent execution model is retained: the model writes Python,
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
model -> exec_py -> persistent Python worker -> computer -> RDP or VNC -> guest GUI
```

RDP and VNC are independent, first-class backends behind `computer`. They do
not share a protocol switch or route through an experimental compatibility
layer.

| Backend | Purpose | Implementation boundary |
| --- | --- | --- |
| RDP | Windows and other RDP targets | Dedicated RDP connection, framebuffer, keyboard, pointer, and session lifecycle |
| VNC/RFB | RFB 3.8 servers with Raw encoding and None or VNC-password authentication | Dedicated RFB negotiation, framebuffer, keyboard, pointer, and session lifecycle |

The repository root is the Codex plugin root. The plugin's preferred MCP tool,
`computer_exec_py`, uses the same persistent worker as the retained Responses
loop. Its globals are `computer`, `Image`, `log`, and `display`; ordinary Python
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

## Tools

Codex receives one protocol-neutral surface: persistent Python execution,
status, connect, screenshot, pointer move/click/drag/scroll, Unicode text, key
chords, bounded action batches, and disconnect. Selecting a backend creates
that backend directly; neither backend is routed through the other.

After `computer_connect`, prefer `computer_exec_py`. For example:

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

VNC supports horizontal and vertical wheel events. RDP supports vertical wheel
events; requesting a horizontal RDP wheel event returns an explicit capability
error instead of silently doing the wrong thing.

## Security model

- Credentials are supplied at runtime and must not be committed.
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

For the complete retained Python framework suite, run
`python-app\.venv\Scripts\python -m pytest -s -p no:cacheprovider
python-app\tests`.

The inherited run lifecycle, cancellation, screenshot/replay storage, and
Responses code loop use the same persistent remote worker. Execution deadlines
terminate the worker process; if an operation is interrupted while input may be
held, a bounded reconnect-and-release helper runs before the session is allowed
to finish. Guest-specific workflows such as opening a URL through the Windows
Run dialog remain outside the protocol-neutral runtime.

## License

MIT License, Copyright (c) 2026 Napreth. The license inherited from the OpenAI
sample repository is preserved in
[`LICENSES/OpenAI-CUA-Sample-App-LICENSE`](LICENSES/OpenAI-CUA-Sample-App-LICENSE).

The original sample README is retained at
[`docs/upstream/openai-sample-README.md`](docs/upstream/openai-sample-README.md)
for architectural reference.
