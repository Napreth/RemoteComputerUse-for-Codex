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

A saved profile cannot be combined with explicit overrides in `computer_connect`. Each connection captures its resolved settings, so later edits do not redirect an existing session or its emergency release connection.

Hosts accept DNS names, ASCII internal names such as `rdp_service`, IPv4 and IPv6 (optionally bracketed). Internationalized names use IDNA2008 with UTS #46 nontransitional processing. Ports are separate fields; URLs, userinfo, paths, query strings, fragments, percent escapes, IPv6 scope identifiers and ambiguous numeric IPv4 forms are rejected.

## Agent-supplied connection fields

Two independent top-level switches in `config.json` default to `false`:

| Switch | Additional MCP tool when enabled |
| --- | --- |
| `enableDeclareAsFillByAgent` | `computer_connect_filled_profile(profile, fields, name=None)` |
| `enableProfileFromAgent` | `computer_connect_from_agent(backend, host, ..., name=None)` |

Restart the MCP server after changing a switch to refresh tool discovery. Each connection call also re-reads the file: disabling a switch immediately prevents new calls through that tool, even if the client has cached its schema. Missing or invalid configuration never enables these tools and does not prevent MCP initialization.

For a saved template, declare the fields Agent may supply with `fillByAgent`. Every declared field must be absent from the saved profile, even if its saved value would be empty, null, false or a default. Required fields such as `host` and RDP `username` may be omitted only when declared. Supply every declared field on each connection; undeclared fields are rejected. The values are validated together with the saved fields and used only for that connection. The server never writes them back to `config.json` or changes process environment variables.

```json
{
  "version": 1,
  "enableDeclareAsFillByAgent": true,
  "enableProfileFromAgent": true,
  "profiles": [
    {"name": "vm-dev", "type": "rdp", "username": "alice", "fillByAgent": ["host", "password"]}
  ]
}
```

With declaration filling enabled, `computer_list_profiles()` returns `fillByAgent` field names alongside each template's name/type, never saved values. Connect using:

```text
computer_connect_filled_profile(profile="vm-dev", fields={"host": "192.0.2.10", "password": "<user-provided password>"}, name="work")
```

Declarations may include only connection fields supported by that profile: `host`, `port`, `password`, RDP `username`/`domain`/`width`/`height`/`insecure_tls`, or VNC `shared`. `name`, `type`, `passwdenv` and `fillByAgent` itself are forbidden. `type` must remain explicitly set to `rdp` or `vnc` in the saved profile and cannot be changed through filling. A template declaring `password` cannot also use `passwdenv`. When the switch is off, declarations are not exposed and ordinary connections require complete saved settings.

With `enableProfileFromAgent` enabled, Agent may provide all connection settings directly without selecting or saving a profile:

```text
computer_connect_from_agent(backend="rdp", host="192.0.2.10", username="alice", password="<user-provided password>", name="work")
```

The direct tool accepts `rdp` or `vnc`, requires `host` and RDP `username`, and uses the same port/dimension/TLS/shared defaults and validation as saved profiles. It does not inherit `REMOTE_COMPUTER_*` values. `name` still identifies the runtime session; it does not create a saved profile. `passwdenv` is not an Agent input. Enabling either feature permits passwords in the corresponding tool arguments and conversation/client history. Existing default tools continue to omit a password argument.

## Existing environment connections

Without `profile`, `computer_connect()` can use values already present in the MCP process environment. The package declares no forwarding list; it cannot supply variables the Codex Host has not injected. This also applies to `passwdenv` and optional uv settings. The bundled MCP user-environment limitation is tracked in [release compatibility](release-compatibility.md).

| Backend | Required variables | Optional variables |
| --- | --- | --- |
| RDP | `REMOTE_COMPUTER_RDP_HOST`, `REMOTE_COMPUTER_RDP_USERNAME` | `REMOTE_COMPUTER_RDP_PASSWORD`, `REMOTE_COMPUTER_RDP_PORT`, `REMOTE_COMPUTER_RDP_DOMAIN`, `REMOTE_COMPUTER_RDP_WIDTH`, `REMOTE_COMPUTER_RDP_HEIGHT`, `REMOTE_COMPUTER_RDP_INSECURE_TLS` |
| VNC | `REMOTE_COMPUTER_VNC_HOST` | `REMOTE_COMPUTER_VNC_PASSWORD`, `REMOTE_COMPUTER_VNC_PORT`, `REMOTE_COMPUTER_VNC_SHARED` |

Set `REMOTE_COMPUTER_BACKEND` to `rdp` (the default) or `vnc`. Explicit arguments inherit environment values only when omitted or `None`; empty targets and usernames are rejected, while `domain=""` clears an inherited RDP domain. Ports and dimensions must be integers in their supported ranges; zero, booleans and floats are rejected. MCP can initialize and list tools without connection settings; an actual connect then reports the missing configuration.

## Startup troubleshooting

The ZIP includes `python-app/.python-version` to request Python 3.12 through uv's normal project discovery; Host-provided `UV_PYTHON` remains an explicit override. If uv fails while querying `.venv/Scripts/python.exe` before MCP initialization, inspect its stderr. A missing `pyvenv.cfg` indicates an incomplete virtual environment, and adding the version file alone cannot repair that existing environment. Stop the plugin, rename only the generated `.venv` shown in the error to a backup name, then restart with the corrected package so uv can rebuild it. When using `UV_PROJECT_ENVIRONMENT`, repair that user-selected environment instead. Keep connection configuration and project sources intact.

On Windows, native dependencies can also fail with “filename or extension is too long” in deeply nested environment paths. This is a dependency-loading failure, not a startup timeout. Use a shorter installation or a shorter `UV_PROJECT_ENVIRONMENT` supplied by the Host when supported. The plugin does not relocate the environment or change Windows path policies.

## Live RDP typing regression

From a source checkout on a Windows Hyper-V host, run `uv run --frozen --project python-app python python-app/scripts/live_rdp_typing_smoke.py --vm-name "<disposable VM>" --profile "<RDP profile>"`. The selected RDP account must also permit PowerShell Direct. The script verifies the target IP, creates a new empty Notepad file per case, types through RDP, saves, and reads back the entire text through PowerShell Direct. Chinese-first input runs before repeated English/Chinese/emoji text, explicit `interval`, tabs/newlines and atomic typing. Use `--initial-only` once per restored guest to isolate the cold-start case. Only CRLF is normalized; no text is trimmed, and a mismatch records its first offset. Results and a screenshot go to `test-artifacts/rdp-notepad-typing.*`. The script does not restore the VM; restore its checkpoint after testing. Font glyphs are not used to judge input integrity.

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
