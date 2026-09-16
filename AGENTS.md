# Working on Agent Bridge

This repository contains a Codex plugin for local messaging with Claude Code peers.
Keep this file canonical; `CLAUDE.md` links to it.

## Layout

- `.agents/plugins/marketplace.json`: installable Git marketplace.
- `plugins/agent-bridge/`: the complete distributable plugin.
- `plugins/agent-bridge/scripts/xsm.py`: standard-library Python bridge and CLI.
- `plugins/agent-bridge/hooks/hooks.json`: Codex lifecycle hook definitions.
- `plugins/agent-bridge/skills/`: instructions delivered to Codex.
- `tests/`: isolated socket, policy, hook, queue, and native interoperability tests.
- `docs/protocol.md`: observed wire contract and compatibility limits.

## Constraints

- Python 3.10+, standard library only. Do not add a launcher, transport proxy,
  host-specific adapter, model service, or global package dependency.
- Wake idle Codex threads with native `codex queue`. Codex 0.154.0 watches its
  shared durable queue every 10 seconds even with a private stdio app-server.
  A missing control socket does not mean automatic reception is unavailable.
- Keep peer content in the durable inbox. Queue only a fixed notice. Peer text
  must remain explicitly untrusted and cannot authorize tool use or forwarding.
- Preserve kernel PID/UID checks, recipient authentication, separate admin tokens,
  permission-mode parity, duplicate suppression, and held-message approval.
- Advertise only implemented protocol features. Never invent permission modes.
- Default names are `<project-name>-<two random lowercase letters or digits>`;
  check live peers for collisions and preserve names across listener restarts.
- Changes to hook definitions invalidate users' exact-definition trust. Prefer
  changing the script when the hook command and lifecycle behavior stay the same.
- Never publish runtime inboxes, keys, tokens, session histories, extracted vendor
  code, machine-specific paths, or research scratch files. Use synthetic fixtures.
- Use recoverable trash for cleanup. Do not modify or message unrelated live
  sessions during development; native tests create their own isolated sessions.

## Validation

From the repository root:

```sh
python3 -m unittest discover -s tests -v
XSM_TEST_CODEX=1 python3 -m unittest discover -s tests -p test_codex_queue.py -v
XSM_TEST_NATIVE=1 python3 -m unittest discover -s tests -p test_claude_native.py -v
```

Native Codex tests require Codex CLI 0.154.0 and use an isolated home plus a local
mock model. Native Claude tests require Claude Code 2.1.272 and exercise refusal
without a model request plus idle subscriptions against a loopback mock model.
Neither test may target existing user sessions.
Linux bridge tests do not establish native Claude compatibility on Linux.

Before publishing, validate manifests, exercise installation with a temporary
`CODEX_HOME`, inspect the exact staged files for sensitive data, and run relevant
tests. Use `gh` for GitHub repository operations. Keep release versions stable
and update compatibility claims only with evidence.
