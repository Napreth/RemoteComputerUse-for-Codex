---
name: remote-computer
description: Operate an isolated graphical computer over the Remote Computer Use plugin's RDP or VNC backend. Use for remote or virtual-machine GUI tasks that must not take over the host desktop.
---

# Remote Computer

Use the plugin's persistent `computer_exec_py` runtime. The configured RDP or
VNC backend is an implementation detail; do not substitute host automation.

1. Call `computer_status`, then `computer_connect` when disconnected.
2. Use `computer_exec_py` and begin with
   `display(computer.screenshot())`. Python globals survive between calls.
3. Combine related actions, screenshot inspection, and bounded loops in one
   code call when useful. The `computer` global provides `move_to`, `click`,
   `drag`, `drag_to`, `scroll`, `write`, `press`, `keypress`/`hotkey`, `status`,
   `screenshot`, and `run_actions`.
4. Treat screenshot pixels as the input coordinate system. The atomic
   `computer_*` tools remain available for simple one-off operations, but do
   not turn a multi-step task into one model turn per input event.
5. Never place credentials in code or tool arguments when the server can read
   them from its environment.
6. Disconnect when the task is complete or when the remote state is uncertain.

RDP and VNC are independent first-class backends. If connection diagnostics
identify a backend-specific failure, report that backend and its actionable
error instead of silently trying the other protocol.
