# Remote Computer Use runtime

This Python package is the bundled Codex MCP runtime. It exposes a persistent `computer_exec_py` session plus convenience Computer API tools, all backed by the same dedicated RDP or VNC connection. It does not import or operate Playwright, PyAutoGUI, or host-desktop input APIs.

## Layout

- `app/backends/rdp`: RDP connection, framebuffer, keyboard, and pointer.
- `app/backends/vnc`: RFB 3.8 Raw framebuffer transport with None or traditional VNC-password authentication, keyboard, and pointer.
- `app/computer_session.py`: protocol-neutral session lifecycle and serialized actions.
- `app/remote`: process-owned persistent Python runtime and synchronous `computer` facade used by model-authored code.
- `app/plugin_session.py`: one worker-owned connection shared by persistent code and atomic MCP tools.
- `app/mcp_server.py`: persistent and atomic MCP tools consumed by Codex.

Credentials come from the client-managed `REMOTE_COMPUTER_RDP_PASSWORD` or `REMOTE_COMPUTER_VNC_PASSWORD` environment variable. They are never accepted as MCP tool arguments, and the runtime does not load a repository `.env` file.

The VNC transport does not claim VeNCrypt/TLS, Tight, ZRLE, proprietary extensions, or enterprise authentication support.

For development, install with `uv sync --project python-app --frozen`, then run the server directly:

```powershell
uv run --frozen --project python-app remote-computer-use
```

The root [MCP manifest](../mcp.json) directly starts `uv run --frozen --no-dev --project ${PLUGIN_ROOT}/python-app remote-computer-use`. Codex expands `${PLUGIN_ROOT}` to the installed plugin root. The plugin does not set uv policy, modify `REMOTE_COMPUTER_*` or `UV_*`, or load `.env` files. Connection values must be supplied by the Codex Host; the root README records the current bundled MCP environment-configuration limitation.

See the root [README](../README.md) for complete configuration and security notes.
