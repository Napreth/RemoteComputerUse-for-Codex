# Python remote architecture

The Python runner retains the upstream Responses loop, HTTP/replay contracts,
Console, and shared lab definitions. Desktop I/O uses a dedicated RDP or VNC
connection. See the [compatibility contract](../../docs/upstream-compatibility.md)
for input semantics and the capabilities that remote protocols cannot provide.

## Execution and ownership

`app/responses_loop.py` sends `exec_py` calls to `app/remote/runtime.py`.
A child process in `app/remote/worker.py` maintains Python globals, `log`,
`display`, and a remote `computer` / `pyautogui` facade between calls.
`app/computer_session.py` serializes input through `app/backends/rdp` or
`app/backends/vnc`. Model code has normal Python privileges; the worker is not
an operating-system security sandbox.

`app/plugin_session.py` owns MCP worker lifetimes. `app/mcp_server.py` exposes
persistent Python and atomic remote actions. `app/scenario_runtime.py` owns a
Console run's remote connection and lab resources. It does not own a guest
browser process or install a guest agent.

## HTTP and saved output

`app/api.py` serves capabilities, scenarios, run admission, stop, SSE, replay,
and screenshots. `app/runner.py` owns run lifecycle and publishes the terminal
state after cleanup and persistence. A cleanup failure blocks further runs
until recovery. `app/models.py` and `contracts/` retain the shared wire shapes;
`X-CUA-Backend` detects a mismatched Console/backend selection.

Python uses the contract's `headful` mode for the remote desktop. The Console
runs against Python; the old JavaScript implementation is a retired reference.
Screenshots contain the remote framebuffer, including guest windows outside
the lab. Browser metadata does not establish observed page navigation.

Version-3 replay bundles are stored in `python-app/data/runs/<run-id>/replay.json`;
lab workspaces live in `python-app/data/workspaces/<run-id>/`. Run completion
means execution and cleanup finished normally, not that the task was graded
successful. Older replay versions remain unchanged and return an unsupported
version error when loaded.

`app/__main__.py` starts the single-process FastAPI service and retains the
exclusive local port lease through shutdown. For configuration, see the
[root README](../../README.md); for checks, see [contributing](contributing.md).
