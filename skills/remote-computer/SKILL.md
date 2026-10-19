---
name: remote-computer
description: Operate an isolated graphical computer through the bundled Remote Computer Use MCP server over RDP or VNC. Use for remote or virtual-machine GUI tasks that must not take over the host desktop.
---

# Remote Computer

Use the bundled `remote_computer` MCP server's persistent
`computer_exec_py` runtime. The configured RDP or VNC backend is an
implementation detail; do not substitute host automation. If the MCP tools are
unavailable, report that its tools are unavailable and inspect startup
diagnostics when accessible. Saved connection profiles are read from the user's
configuration directory; use `computer_list_profiles` for discovery. Do not read
the raw file with model code or create credential files on the user's behalf.

1. Call `computer_list`. When the intended host is absent, call
   `computer_list_profiles` and connect with `profile="...", name="..."`.
   Profile names select saved connections; `name` identifies a running session.
   Do not combine a profile with host/backend/username/port overrides.
   If discovery returns `fillByAgent`, use the available
   `computer_connect_filled_profile(profile="...", fields={...}, name="...")`
   with exactly those fields. Use only user-provided values; do not guess
   credentials or change undeclared fields. `name` and `passwdenv` cannot be
   filled. When `computer_connect_from_agent` is available, the user has enabled
   direct settings: use it for a user-specified RDP/VNC target and credentials.
   Both paths apply only to that connection and never save a profile. Do not
   edit configuration or enable either feature on the user's behalf.
   A missing Profiles file returns an empty list. For an intended target
   configured through the existing environment, then try
   `computer_connect(name="...")` without `profile`; profile discovery is not a
   prerequisite for legacy environment connections. If the user explicitly
   requests that mode, call it directly. Only ask the user to configure a target
   when the connect error reports missing connection settings. Report network
   or authentication errors as such. An invalid Profiles file is an error;
   do not silently ignore it or choose a different host. Explicit connection
   arguments remain available for user-specified targets.
   An MCP server instance may own several named hosts. Only the first
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
5. Credentials normally come from user-managed profiles or the existing server
   environment. Never place them in Python code. Password tool arguments are
   permitted only through the explicitly enabled Agent connection tools, using
   user-provided credentials. Do not echo passwords in output. A profile file
   containing plaintext passwords remains readable by same-account code; do not
   describe this as credential isolation from `computer_exec_py`.
6. Treat `computer_exec_py` as full Python execution under the MCP server's OS
   account, not as a filesystem, process, or network sandbox.
7. Disconnect when the task is complete or when the remote state is uncertain.

When only one host is connected, `session` may be omitted. When several are
connected, pass the intended `session` to status, execution, screenshot,
atomic action, batch, and disconnect tools. Never guess among multiple hosts.

RDP and VNC are independent first-class backends. If connection diagnostics
identify a backend-specific failure, report that backend and its actionable
error instead of silently trying the other protocol.
