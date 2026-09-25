# Upstream compatibility contract

Baseline: OpenAI sample commit `f2a3dc523ae406f9b704f9a420a05402a63b4522`.
This fork keeps the Python Responses loop, persistent globals, HTTP/replay
contracts, Console and lab documents. It replaces local desktop I/O with RDP or
VNC and adds named MCP sessions. It does not promise full PyAutoGUI equivalence.

## Python input API

`computer` and `pyautogui` use the same remote connection. Importing `pyautogui`
inside the worker resolves to the remote facade, never the host library.

Supported common calls include `size`, `position`, `onScreen`, `screenshot`
(including `imageFilename` and `region`), `moveTo`/`move_to`, `move`/`moveRel`,
`click` with repeated clicks, interval, duration and tween, `doubleClick`, `tripleClick`,
`rightClick`, `leftClick`, `middleClick`, `mouseDown`/`mouseUp`, `dragTo`/`drag_to`, relative `drag`/`dragRel`,
`write`/`typewrite` with string or key-name list and interval, sequential `press` with presses and interval,
`hotkey` with interval, `keyDown`/`keyUp`, and the `hold` context manager.
Pointer actions accept two-coordinate tuples. `hold` releases in input order;
`hotkey` releases in reverse order, matching [PyAutoGUI 0.9.54](https://github.com/asweigart/pyautogui/blob/master/pyautogui/__init__.py).
Movement supports duration and a callable tween. `PAUSE` defaults to 0.1 seconds.
`mouseDown` and `mouseUp` accept `duration`/`tween` but move immediately, as in
PyAutoGUI 0.9.54.
Path-based dragging remains available as `drag_path` and `drag(path, ...)`.

Python `scroll`/`vscroll` use wheel clicks, positive up and negative down;
`hscroll` uses positive right. Requests over 20 clicks are split, not truncated.
Atomic MCP actions retain their documented protocol deltas, positive down/right
and 120 units per wheel click. RDP currently rejects horizontal scrolling.

Successful Python calls may keep keys or mouse buttons held across calls.
Health replies preserve this state so normal close, transport failure and
execution timeout still trigger input release. Unacknowledged input failures
remain pending until a successful release. A Python thread that outlives a tool
call also retains cleanup responsibility for the worker's lifetime; close may
therefore open a release connection even when the latest input snapshot is idle.
Before Unicode input is sent, the worker reports UTF-16 code units (RDP) or
keysyms (VNC) over its private pipe. The parent retains these identities only in
memory, drains the pipe after worker exit, and passes them to the release helper
through stdin. The helper releases these exact identities in addition to the
standard key/button sweep. RDP control-character scancodes send an empty risk
frame before key-down; the helper releases them through its scancode sweep
without interpreting them as Unicode identities. If identity data is incomplete,
the helper still releases all known inputs and sweeps standard keys/buttons,
but cleanup remains unconfirmed. No input identity journal is written to disk.

The PNG display budget is the upstream aggregate 8 MiB of PNG bytes, before
Base64 encoding.

## Explicit remote limits

- RDP `press`, `keyDown`/`keyUp`, `hotkey`, and `typewrite` with a key-name list
  map printable characters to US keyboard-layout scancodes. They cannot query
  the guest keyboard layout, so a non-US guest may produce different characters
  or shortcuts. RDP `write`/`typewrite` with a string sends ordinary characters
  as Unicode for layout-independent text entry. `\t`, `\n` and `\b` use
  Tab, Return and Backspace scancodes in both string typing and key calls.
  The `\r` literal is a no-op, matching PyAutoGUI 0.9.54 on Windows; use
  `\n`, `enter` or `return` to send Return.
- VNC string typing and key calls resolve Tab, Return, Backspace, Escape and
  Delete literals to X11 keysyms. Other C0/C1 control characters fail
  explicitly rather than sending invalid KeyEvent values.
- The facade exposes `LEFT`, `MIDDLE`, `RIGHT`, `PRIMARY`, and `SECONDARY`.
  Button arguments accept these names case-insensitively and numbers 1/2/3.
  `primary` and `secondary` assume the guest has not swapped its mouse buttons;
  RDP/VNC do not expose that guest setting. Use `left` or `right` for a physical
  button.
- `position()` is the last successfully sent pointer position, initially (0, 0).
  RDP/VNC do not provide an authoritative query of another user's mouse position.
- There is no host-corner PyAutoGUI fail-safe. Use the runner's Cancel control or
  cancel/disconnect the MCP session. Setting a `FAILSAFE` attribute cannot add a
  physical pointer sensor to these protocols.
- Image-location helpers, desktop dialogs, undocumented PyAutoGUI internals,
  platform-specific key aliases and the entire PyAutoGUI package are not
  implemented. Unsupported methods fail explicitly; they never fall back to
  host desktop control. Unicode text uses `write`; held VNC character keys are
  limited to printable ASCII, supported control literals and named keys.
- Remote coordinates must be inside the framebuffer. Image pixels and input
  coordinates share one coordinate space.
- Labs use the configured Windows guest's Edge via Win+R. VNC requires an
  explicitly verified shortcut mapping and
  `REMOTE_COMPUTER_VNC_WINDOWS_NAVIGATION=win-r`; there is no coordinate fallback. There is no owned
  guest browser process or temporary browser profile. Disconnecting a run does
  not close unrelated guest windows or erase the guest browser profile.
- Replay `currentUrl` is the last URL requested by the runner, not a browser
  navigation observation; page title is unavailable. Screenshots capture the
  remote framebuffer, rather than a browser-only preview.
- Lab readiness acknowledges delivery of the token-protected page to an allowed
  guest peer, not completed JavaScript rendering.

No guest agent or host browser automation is installed to simulate capabilities
that the remote protocols do not expose.
