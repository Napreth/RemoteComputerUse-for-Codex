# Configuration reference

See the [README](../README.md#configure) for configuration file locations and a minimal example.

## Profiles

The file uses integer `version: 1` and a `profiles` array. Profile names must be unique. Unknown fields, duplicate JSON properties, malformed files and files larger than 1 MiB are rejected without echoing their contents. A missing file produces an empty `computer_list_profiles()` result; explicitly requesting a profile from it reports an error.

| Type | Required fields | Optional fields and defaults |
| --- | --- | --- |
| RDP | `name`, `type: "rdp"`, `host`, `username` | `port: 3389`, `domain: ""`, `width: 1440`, `height: 900`, `insecure_tls: false` |
| VNC | `name`, `type: "vnc"`, `host` | `port: 5900`, `shared: true` (false is unsupported) |
| Both | — | `password: ""`, or `passwdenv` naming an existing variable |

`passwdenv` accepts letters, digits and underscores and cannot start with a digit. It is mutually exclusive with `password`; a missing referenced variable is an error. Referenced credential variables are removed from worker environments before launch, while the parent retains the resolved credentials for cleanup. This does not isolate credentials from same-account Python.

A profile cannot be combined with explicit connection overrides. Each connection captures its resolved settings, so later edits do not redirect an existing session or its emergency release connection.

Hosts accept DNS names, ASCII internal names such as `rdp_service`, IPv4 and IPv6 (optionally bracketed). Internationalized names use IDNA2008 with UTS #46 nontransitional processing. Ports are separate fields; URLs, userinfo, paths, query strings, fragments, percent escapes, IPv6 scope identifiers and ambiguous numeric IPv4 forms are rejected.

## Existing environment connections

Without `profile`, `computer_connect()` can use values already present in the MCP process environment. The package declares no forwarding list; it cannot supply variables the Codex Host has not injected. This also applies to `passwdenv` and optional uv settings. The bundled MCP user-environment limitation is tracked in [release compatibility](release-compatibility.md).

| Backend | Required variables | Optional variables |
| --- | --- | --- |
| RDP | `REMOTE_COMPUTER_RDP_HOST`, `REMOTE_COMPUTER_RDP_USERNAME` | `REMOTE_COMPUTER_RDP_PASSWORD`, `REMOTE_COMPUTER_RDP_PORT`, `REMOTE_COMPUTER_RDP_DOMAIN`, `REMOTE_COMPUTER_RDP_WIDTH`, `REMOTE_COMPUTER_RDP_HEIGHT`, `REMOTE_COMPUTER_RDP_INSECURE_TLS` |
| VNC | `REMOTE_COMPUTER_VNC_HOST` | `REMOTE_COMPUTER_VNC_PASSWORD`, `REMOTE_COMPUTER_VNC_PORT`, `REMOTE_COMPUTER_VNC_SHARED` |

Set `REMOTE_COMPUTER_BACKEND` to `rdp` (the default) or `vnc`. Explicit arguments inherit environment values only when omitted or `None`; empty targets and usernames are rejected, while `domain=""` clears an inherited RDP domain. Ports and dimensions must be integers in their supported ranges; zero, booleans and floats are rejected. MCP can initialize and list tools without connection settings; an actual connect then reports the missing configuration.

## Retained Labs workflow

Labs run from the source checkout's runner and Console; they are not bundled in the upload ZIP. Windows navigation requires Win+R and Edge. Set `REMOTE_COMPUTER_GUEST_OS=windows`; for VNC, verify that the server forwards Win+R and explicitly set `REMOTE_COMPUTER_VNC_WINDOWS_NAVIGATION=win-r`. Other shortcut mappings are unsupported; navigation uses no fixed taskbar coordinates.

The runner serves the selected lab on a guest-reachable interface. Route detection is automatic; use these overrides for unusual networking:

| Variable | Purpose |
| --- | --- |
| `REMOTE_COMPUTER_LAB_HOST` | Host address placed in the guest URL |
| `REMOTE_COMPUTER_LAB_BIND_HOST` | Interface on which the lab server listens |
| `REMOTE_COMPUTER_LAB_PORT` | Port; `0` chooses an ephemeral port |
| `REMOTE_COMPUTER_LAB_GUEST_HOST` | Expected HTTP source address when NAT or forwarding changes it |

Allow guest access through the host firewall. Each run uses a fresh 128-bit capability path and accepts only the resolved guest source addresses; forwarded HTTP headers do not override this check. Keep the URL private while the run is active. Readiness confirms a successful guest GET, not completed JavaScript rendering. The live lab smoke captures a subsequent framebuffer for manual inspection; it does not assert that the selected lab is visually rendered.
