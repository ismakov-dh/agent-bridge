# Agent Bridge

A Codex plugin that lets local **Codex and Claude Code sessions message each other
by name**. Codex sessions appear in Claude Code’s `ListAgents`, receive messages in
a durable inbox, and wake automatically when idle.

No launcher or host-specific integration. Python’s standard library handles the local
peer protocol; native `codex queue` handles automatic reception.

## Requirements

- **macOS**: tested with Codex CLI **0.154.0**, Claude Code **2.1.272**, and Python **3.10+**.
- `codex`, `claude`, and `python3` available on PATH.
- Both agents running as the same local user, using the same Claude registry.
- Codex lifecycle hooks enabled and this plugin’s hooks trusted.

The Claude peer protocol is unofficial and can change between versions. Linux socket
support is implemented and covered by bridge tests, but native Claude interoperability
has only been verified on macOS. Windows is not supported.

## Install

```sh
codex plugin marketplace add ismakov-dh/agent-bridge
codex plugin add agent-bridge@agent-bridge
codex features enable hooks
```

Open Codex’s **`/hooks`** screen and review/trust the plugin’s five hooks. Then start
or resume a thread. Installation alone does not grant hook trust. If your editor does
not expose `/hooks`, use Codex CLI with the same `CODEX_HOME` to review the hooks.

Each thread registers automatically as `<project-name>-<two random characters>`, for
example `my-project-k7`. Names are checked against live peers for collisions and survive
listener restarts. No Claude plugin or socket configuration is needed.

## Use it

Ask Codex:

> What is my messaging session name, and which peers are available?

> Send “Ready for review” to the session named my-project-k7.

> Check my inbox.

Ask Claude Code:

> Use ListAgents to find my-project-k7, then SendMessage to it with “Hello from Claude”.

Use the actual registered name returned by `ListAgents` or the plugin’s `status`
command. Session names, UUIDs, and PIDs are supported; ambiguous names are rejected.

Incoming messages are treated as peer data. They do not authorize unrelated actions,
forwarding, or tool use. When permission modes differ, messages are held for explicit
approval instead of delivered automatically.

## Automatic reception

The listener stores each incoming message, then invokes `codex queue` with a fixed
inbox notice for that same thread. The message body stays in the inbox until a hook
or an explicit read delivers it as untrusted data.

Codex’s built-in watcher checks the shared durable queue every **10 seconds**. This
works in an ordinary private stdio session, without a shared daemon. An available
native daemon can deliver sooner. Active work or queued turns can add delay;
interrupted threads remain paused, and unloaded threads receive notices on resume.

The listener and receiving Codex process must use the same `CODEX_HOME` and SQLite
configuration. Hooks normally inherit these. A successful queue submission is not an
acknowledgement that the model has read or acted on the message.

## Command-line reference

Agents use the script bundled with the installed skill. For development, run it from
this checkout:

```sh
python3 plugins/agent-bridge/scripts/xsm.py list
python3 plugins/agent-bridge/scripts/xsm.py status
python3 plugins/agent-bridge/scripts/xsm.py send --to my-project-k7 --message 'Hello'
python3 plugins/agent-bridge/scripts/xsm.py inbox
python3 plugins/agent-bridge/scripts/xsm.py inbox --consume
```

`CODEX_THREAD_ID` selects the current thread. From a separate terminal, add
`--thread <Codex-thread-UUID>`. Use `--message-file /path/to/message.txt` for multiline
messages. `sent` confirms a socket write, not model delivery; inspect control receipts
for holds or refusals.

| Command | Purpose |
| --- | --- |
| `list` | Discover live registered peers |
| `status` | Show this thread’s name, permissions, and queue availability/result |
| `rename --name review-session` | Set an explicit name without restarting |
| `rename` | Restore a project-derived name |
| `inbox` | Peek at pending and held messages |
| `inbox --consume` | Consume the next eight pending messages |
| `inbox --all --offset 8` | Read retained history, eight messages at a time |
| `accept --id <message-UUID>` | Release a held message after approval |
| `deny --id <message-UUID>` | Reject a held message |
| `stop` | Stop this thread’s listener and remove its registration |

Drain `inbox --consume` repeatedly with offset zero until empty. History is retained
for recovery if hook output is lost after consumption. `start` exists for diagnostics;
only specify `--permission-mode` when it matches the actual Codex session.

## Configuration and storage

Set these before starting Codex or its listener:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude registry; both agents must share it |
| `XSM_DATA_DIR` | `~/.codex/cross-session-messaging` | Private inboxes, listener state, and logs |
| `XSM_INBOUND` | `parity` | `parity`, `hold`, `refuse`, or explicit `accept` policy |
| `XSM_WAKE` | `auto` | Set `off` to disable automatic wake notices |
| `XSM_CODEX_BIN` | `codex` | Executable used for native queue submission |
| `XSM_CODEX_REMOTE` | unset | Optional explicit native queue endpoint |

Peer keys and admin tokens are generated locally. The listener checks kernel PID/UID
identity and uses a separate admin token that is never published to peers. The plugin
has no cloud relay and adds no model-service credentials. Normal Codex turns still
use your existing Codex provider and may incur its usual usage costs.

`SessionEnd` stops the listener; an owner-process check also cleans up after Codex exits.
Inbox history survives listener restarts; history and logs are not automatically
pruned. Discovery ignores dead/recycled PIDs.
Attachments, remote-host messaging, idle subscriptions, and peer lifecycle control
are not implemented.

## Troubleshooting

- **Missing from ListAgents:** confirm hook trust, start/resume a Codex thread, then
  check `status`. Both tools must use the same `CLAUDE_CONFIG_DIR`.
- **Message held:** inspect `inbox` and the two sessions’ permission modes; approve
  deliberately with `accept`. Do not invent a mode to bypass parity checks.
- **Stored but not waking:** check `status` → `autoReceive.lastError`, Codex CLI
  compatibility, and matching Codex home/SQLite configuration. Allow a watcher interval.
- **No registered listener:** inspect the hook output or per-thread `daemon.log`
  under `XSM_DATA_DIR`. Do not share runtime keys or inbox files publicly.

## Development

```sh
python3 -m unittest discover -s tests -v
XSM_TEST_CODEX=1 python3 -m unittest discover -s tests -p test_codex_queue.py -v
XSM_TEST_NATIVE=1 python3 -m unittest discover -s tests -p test_claude_native.py -v
```

The standard suite needs no packages or model service. Native tests create isolated
sessions: Codex uses a loopback mock model, and Claude exercises a refusal path without
invoking a model. Tests never message existing user sessions.

To install this checkout locally:

```sh
codex plugin marketplace add .
codex plugin add agent-bridge@agent-bridge
```

See [AGENTS.md](AGENTS.md) for contribution constraints and [the protocol notes](docs/protocol.md)
for the wire format. Licensed under [MIT](LICENSE). This project is not affiliated
with OpenAI or Anthropic.
