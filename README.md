# Remote Computer Use for Codex

Remote Computer Use is a fork of OpenAI's Computer Use sample applications that
is being rebuilt as an MIT-licensed Codex plugin for controlling isolated
computers over remote-desktop protocols.

This is a replacement product, not an additional backend for the original
local Computer Use sample. This first fork change removes only the concrete
Playwright and PyAutoGUI host-desktop implementations and their backend-specific
tests. The reusable orchestration framework remains: the Responses code loop,
shared contracts, run and lease lifecycle management, cancellation and
recovery, the operator console, replay, labs, and the scenario harness.

The upstream execution model is a design constraint for the replacement. The
model will continue to write code into a persistent run-scoped runtime so one
call can combine actions, screenshots, state inspection, and loops. RDP and VNC
replace the local screen/input transport; they do not replace that agent model.

## Architecture

The target architecture keeps the upstream control flow and changes its final
transport boundary:

```text
model -> exec_py -> persistent runtime -> remote computer -> RDP or VNC
```

RDP and VNC are independent, first-class backends. They do not share a protocol
switch or route through an experimental compatibility layer.

| Backend | Purpose | Implementation boundary |
| --- | --- | --- |
| RDP | Windows and other RDP targets | Dedicated RDP connection, framebuffer, keyboard, pointer, and session lifecycle |
| VNC | Standard RFB/VNC targets | Dedicated VNC negotiation, framebuffer, keyboard, pointer, and session lifecycle |

The repository root is the Codex plugin root. Plugin manifests, MCP tools, and
the remote-computer skill are developed alongside the retained console and test
framework so both backends can be exercised through the same observable run
lifecycle.

## Security model

- Credentials are supplied at runtime and must not be committed.
- Remote screenshots and input travel through the selected remote protocol;
  the host computer's pointer and keyboard are not controlled.
- Mutating tools remain approval-gated by Codex.
- VNC deployments should prefer a protected network or an authenticated tunnel.

## Development

Development happens directly on `main`. Repository commits are intentionally
left unsigned during implementation so the maintainer can sign the finished
history later.

## License

MIT License, Copyright (c) 2026 Napreth. The license inherited from the OpenAI
sample repository is preserved in
[`LICENSES/OpenAI-CUA-Sample-App-LICENSE`](LICENSES/OpenAI-CUA-Sample-App-LICENSE).

The original sample README is retained at
[`docs/upstream/openai-sample-README.md`](docs/upstream/openai-sample-README.md)
for architectural reference.
