---
name: remote-computer
description: Operate an isolated graphical computer over the Remote Computer Use plugin's RDP or VNC backend. Use for remote or virtual-machine GUI tasks that must not take over the host desktop.
---

# Remote Computer

Use the plugin's persistent `computer_exec_py` runtime. The configured RDP or
VNC backend is an implementation detail; do not substitute host automation.

1. Call `computer_list`. Connect a host with a stable `name` when it is absent.
   A plugin-server instance may own several named hosts. Only the first
   connection may omit `name`; later connections must name their identity.
2. Use `computer_exec_py` and begin with
   `display(computer.screenshot())`. Python globals survive between calls.
3. Combine related actions, screenshot inspection, and bounded loops in one
   code call when useful. The `computer` global provides `move_to`, `click`,
   `drag`, `drag_to`, `scroll`, `write`, `press`, `keypress`/`hotkey`, `status`,
   `screenshot`, and `run_actions`.
   `pyautogui` and `import pyautogui` resolve to the same remote compatibility
   API. Python `scroll` uses wheel clicks (positive up, negative down); atomic
   scroll actions use deltas (positive down/right, 120 per click). `press` sends
   sequential keys; `hotkey` holds a chord. Common duration, interval, repeat,
   key/button hold and screenshot-file arguments are supported; see
   `docs/upstream-compatibility.md` for the explicit limits.
4. Treat screenshot pixels as the input coordinate system. The atomic
   `computer_*` tools remain available for simple one-off operations, but do
   not turn a multi-step task into one model turn per input event.
5. Never place credentials in code or tool arguments when the server can read
   them from its environment.
6. Treat `computer_exec_py` as full Python execution under the MCP server's OS
   account, not as a filesystem, process, or network sandbox.
7. Disconnect when the task is complete or when the remote state is uncertain.

When only one host is connected, `session` may be omitted. When several are
connected, pass the intended `session` to status, execution, screenshot,
atomic action, batch, and disconnect tools. Never guess among multiple hosts.

RDP and VNC are independent first-class backends. If connection diagnostics
identify a backend-specific failure, report that backend and its actionable
error instead of silently trying the other protocol.
