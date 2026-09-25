# Upstream compatibility contract

Baseline: OpenAI sample commit `f2a3dc523ae406f9b704f9a420a05402a63b4522`.
This fork keeps the Python Responses loop, persistent globals, HTTP/replay
contracts, Console and lab documents. It replaces local desktop I/O with RDP or
VNC and exposes an MCP server. It does not promise full PyAutoGUI equivalence.

## Python input API

`computer` and `pyautogui` use the same remote connection. Importing `pyautogui`
inside the worker resolves to the remote facade, never the host library.

Supported common calls include `size`, `position`, `onScreen`, `screenshot`
(including `imageFilename` and `region`), `moveTo`/`move_to`, `move`/`moveRel`,
`click` with repeated clicks and interval, `doubleClick`, `tripleClick`,
`rightClick`, `mouseDown`/`mouseUp`, `dragTo`/`drag_to`, relative `drag`/`dragRel`,
`write`/`typewrite` with interval, sequential `press` with presses and interval,
`hotkey` with interval, `keyDown`/`keyUp`, and the `hold` context manager.
Movement supports duration and a callable tween. `PAUSE` defaults to 0.1 seconds.
Path-based dragging remains available as `drag_path` and `drag(path, ...)`.

Python `scroll`/`vscroll` use wheel clicks, positive up and negative down;
`hscroll` uses positive right. Requests over 20 clicks are split, not truncated.
Atomic MCP actions retain their documented protocol deltas, positive down/right
and 120 units per wheel click. RDP currently rejects horizontal scrolling.

Successful Python calls may keep keys or mouse buttons held across calls.
Health replies preserve this state so normal close, transport failure and
execution timeout still trigger input release. The PNG display budget is the
upstream aggregate 8 MiB of PNG bytes, before Base64 encoding.

## Explicit remote limits

- `position()` is the last successfully sent pointer position, initially (0, 0).
  RDP/VNC do not provide an authoritative query of another user's mouse position.
- There is no host-corner PyAutoGUI fail-safe. Use the runner's Cancel control or
  cancel/disconnect the MCP session. Setting a `FAILSAFE` attribute cannot add a
  physical pointer sensor to these protocols.
- Image-location helpers, desktop dialogs, undocumented PyAutoGUI internals,
  platform-specific key aliases and the entire PyAutoGUI package are not
  implemented. Unsupported methods fail explicitly; they never fall back to
  host desktop control. Unicode text uses `write`; held VNC character keys are
  limited to printable ASCII and named keys.
- Remote coordinates must be inside the framebuffer. Image pixels and input
  coordinates share one coordinate space.
- This correction does not provide guest browser navigation. There is no owned
  guest browser process or temporary browser profile. Disconnecting a run does
  not close unrelated guest windows or erase the guest browser profile.
- Replay `currentUrl` is the last URL requested by the runner, not a browser
  navigation observation; page title is unavailable. Screenshots capture the
  remote framebuffer, rather than a browser-only preview.

No guest agent or host browser automation is installed.
