# Remote Computer Use for Codex

English | [简体中文](README_zh.md)

A Codex plugin based on OpenAI's Computer Use sample. It replaces local desktop input with RDP and VNC while keeping persistent Python execution: variables survive between calls, and one call can combine screenshots, actions and loops. Each named connection has its own worker and input state.

## Install

Install Codex, Python 3.12 and `uv`, with `uv` on `PATH`. Download `remote-computer-use-1.0.0.zip` from **Assets** on the [Releases page](https://github.com/Napreth/RemoteComputerUse-for-Codex/releases). In Codex, open **Customize** from the sidebar, select **Plugins → Add → Upload plugin ZIP**, and choose the archive without extracting it. The Plugins page reached through Settings may not show the upload option.

Codex starts the bundled `remote_computer` MCP server using `uv` and the project's lockfile. The first start may download dependencies; current startup compatibility results are tracked in [release compatibility](docs/release-compatibility.md).

## Configure

Create `config.json` yourself at the following location. The plugin does not create or migrate configuration files.

| Platform | Path |
| --- | --- |
| Windows | `%LOCALAPPDATA%\remote-computer-use\config.json` |
| Linux, absolute `XDG_CONFIG_HOME` set | `$XDG_CONFIG_HOME/remote-computer-use/config.json` |
| Linux, otherwise | `$HOME/.remote-computer-use/config.json` |

```json
{
  "version": 1,
  "profiles": [
    {"name": "vm-dev", "type": "rdp", "host": "192.0.2.10", "username": "alice", "password": "<your-password>"},
    {"name": "vm-test", "type": "vnc", "host": "192.0.2.11", "passwdenv": "VM_TEST_PASSWORD"}
  ]
}
```

Use unique profile names and either `password` or `passwdenv`. The latter names an existing MCP-process environment variable; it does not request forwarding from Codex. RDP requires `host` and `username` and defaults to port 3389 with a 1440×900 screen. VNC requires `host` and defaults to port 5900; only shared sessions are supported. Use a hostname or IP address, with the port in a separate field.

Profiles are reread for each connection; edits do not change existing sessions. Existing `REMOTE_COMPUTER_*` connections remain supported without `profile`. The runtime does not load `.env` or set `UV_*` defaults. See [configuration reference](docs/configuration.md) for optional fields, environment variables and Labs setup.

Agent-supplied settings are optional and disabled by default. Set `enableDeclareAsFillByAgent: true` at the top level to let Agent fill a profile's declared fields, such as `fillByAgent: ["host", "password"]`; those fields must be absent from the profile. `name`, `type` and `passwdenv` cannot be filled. Set `enableProfileFromAgent: true` to expose a direct connection tool. Values apply only to that connection and are never saved. See [Agent-supplied connection fields](docs/configuration.md#agent-supplied-connection-fields) for examples.

## Use

Start a Codex conversation and ask it to use a configured remote computer. The basic tool flow is:

```text
computer_list_profiles()
computer_connect(profile="vm-dev", name="work")
computer_exec_py(session="work", code="display(computer.screenshot())")
computer_disconnect(session="work")
```

`profile` selects saved settings; `name` identifies the running session. Only the first connection may omit `name` (it becomes `default`). Operations may omit `session` when exactly one computer is connected. Use `computer_list()` and `computer_status()` to inspect sessions.

Inside `computer_exec_py`, the globals `computer`, `pyautogui`, `Image`, `log` and `display` are available. For example:

```python
display(computer.screenshot())
computer.click(420, 300)
computer.write("Hello")
display(computer.screenshot())
```

`import pyautogui` uses the remote facade. Screenshots return Pillow images in the input coordinate space. Atomic tools such as `computer_click`, `computer_screenshot` and `computer_run_actions` use the same connection. Supported calls, scroll conventions and backend limitations are documented in the [compatibility contract](docs/upstream-compatibility.md).

## Security and recovery

`computer_exec_py` runs full Python under the MCP server's OS account; **it is not a sandbox**. RDP/VNC input targets the remote desktop, but Python can access host files, processes and networks. Plaintext profile passwords are therefore readable by same-account code. Keep configuration outside the repository and ZIP, restrict its permissions, and use an isolated account or VM for untrusted code. Passwords are excluded from default tools; enabling Agent-supplied settings permits them in tool arguments and conversation/client history.

VNC supports RFB 3.8 with Raw encoding and None/VNC-password authentication, without TLS; use a protected network or tunnel. Interrupted input triggers bounded release attempts. If cleanup fails, recover the remote input state and restart the plugin server before reusing the blocked session name.

## Development

Build an upload ZIP with `python scripts/package_plugin.py` (Python 3.12+, standard library only) or `pnpm plugin:pack`. Output defaults to `dist/remote-computer-use-1.0.0.zip`; pass `--output <path>.zip` to choose another destination. The archive includes the bundled MCP runtime and docs, excludes local configuration and development artifacts, and uses fixed ZIP metadata for repeatable builds.

Run `pnpm check` for Python, Console, contracts, Labs and build checks. `pnpm dev:python` starts the retained runner and Console; `pnpm start:python` uses the built Console. The old JavaScript/Playwright backend is retired.

## License

[MIT](LICENSE), Copyright (c) 2026 Napreth. The inherited [OpenAI license](LICENSES/OpenAI-CUA-Sample-App-LICENSE) and [sample README](docs/upstream/openai-sample-README.md) are retained.
