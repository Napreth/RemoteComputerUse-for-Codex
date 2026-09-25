# Contributing to the Python remote backend

Run commands from the repository root. Install Node/pnpm and uv, then:

```bash
pnpm install --frozen-lockfile
pnpm python:install
pnpm check
node --test scripts/launch.test.mjs
```

`pnpm check` checks the Python runtime, shared contracts, Console and shared lab
logic, then builds the Console. Ordinary tests use fake transports and safe
subprocesses; they do not send real desktop input or call the live model API.
The retired JavaScript backend and its browser tests are excluded from the
active workspace and CI.

## Development and production

Configure an RDP or VNC target and credentials as described in the
[root README](../../README.md), then use `pnpm dev:python`. For production,
run `pnpm build` followed by `pnpm start:python`. The Python service defaults
to port 4041 and the Console to 3000. Use the single-process entrypoint;
Ctrl+C waits for worker and input cleanup before releasing the service lease.

## Remote checks

Live checks require an explicitly configured disposable guest. The scripts
under `python-app/scripts/live_remote*` exercise remote input and screenshots;
inspect each script's options and requirements before running it. No live
check should control the host desktop. Restore the guest checkpoint after
completion when using the development VM.

Record which protocols and operating systems were actually tested. Inspect
screenshots to judge task success. A delivered page or valid PNG alone does
not establish that the selected lab rendered correctly. There is no local
PyAutoGUI corner fail-safe; use Stop or cancel/disconnect the remote session.

## Change locations

- `app/responses_loop.py`: model and tool interaction.
- `app/remote/`, `app/computer_session.py`, `app/backends/`: execution and I/O.
- `app/plugin_session.py`: MCP connection ownership and cleanup.
- `contracts/` and `app/models.py`: matching wire types and fixtures.
- `console/` and `labs/`: retained operator interface and lab content.

Preserve terminal-after-cleanup behavior and the documented
[upstream compatibility boundary](../../docs/upstream-compatibility.md).
