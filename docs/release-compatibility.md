# 1.0 release compatibility

**Status on 2026-10-15: 1.0 release blocked by cold-start timing.** A clean Windows 11 Hyper-V guest with Python already installed, no project virtual environment, no connection environment, and no `UV_*` overrides exceeded the shared startup deadline at 30.016 seconds while dependency downloads were still pending. MCP initialization did not complete. This is a failed startup gate, not a successful fresh install.

After dependencies were installed, the same guest completed startup in 3.531 seconds and discovered all 14 tools. Without `LOCALAPPDATA` or `REMOTE_COMPUTER_*` in the MCP environment, it found the real Windows user configuration directory, returned only profile names/types, reread file edits, reported a missing `passwdenv`, and cleaned up a refused localhost connection without returning the profile password. These passes establish Profiles integration, not cold-install readiness.

The bundled MCP command remains direct `uv run --frozen --no-dev --project ${PLUGIN_ROOT}/python-app remote-computer-use`. No Python launcher, environment defaults, forwarding list, `.env` loader, or uv policy is added.

## Connection configuration

User-owned Profiles files provide connection settings without bundled MCP transport overrides. A plaintext `password` works without Host environment injection; `passwdenv` requires the referenced variable to already exist in the MCP process. The plugin cannot make Codex forward it. Upstream [issue #36854](https://github.com/openai/codex/issues/36854) remains relevant to environment-based credentials and optional uv overrides, but does not prevent plaintext profile connections. Plaintext files are not isolated from same-account Python.

## Startup release gate

The [Codex main client](https://github.com/openai/codex/blob/main/codex-rs/codex-mcp/src/rmcp_client.rs) checked on 2026-10-15 uses a 30-second startup default and 300-second tool default. The package regression applies one shared 30-second deadline from spawning uv through completion of MCP initialization; tool calls keep 300 seconds. Delays in either spawning or initialization consume that same budget.

The actual opt-in pytest cold-cache check also failed with a startup `TimeoutError` on Windows; its 34.44-second total included process cleanup after the 30-second deadline. Cleanup time is not additional time allowed for initialization.

1.0 must remain blocked until supported first-install conditions reliably complete that startup gate. A warm-cache pass is not a cold-install pass. The package does not expose a timeout override, and increasing the test timeout is not a workaround.

Run the cold-cache gate with `REMOTE_COMPUTER_COLD_INSTALL_TEST=1 uv run --frozen --project python-app pytest -s -p no:cacheprovider python-app/tests/test_mcp_server.py -k cold-cache`. It creates a fresh project environment and an empty dependency cache. Python itself may already be installed, as documented in the installation prerequisites. A timeout fails this test; it is not marked expected-failure. Regular CI also runs fresh-environment checks with a reusable dependency cache through `REMOTE_COMPUTER_FRESH_INSTALL_TEST=1`.
