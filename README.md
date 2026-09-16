# Agent Bridge

A Codex plugin for delegating work and sharing context between local **Codex and
Claude Code sessions**.

## Use it

Just ask Codex in ordinary language:

> Ask the backend session to implement the new authentication endpoint.

> Send context of current changes to the frontend session.

> Ask the backend session whether the API changes are ready.

Agent Bridge activates for these requests, finds the matching running session, and
sends the task or relevant context. Use a project or session name such as `backend`
or `frontend`; Codex resolves its registered name. If several sessions match, Codex
asks which one you mean.

For a task, it sends the requirements and relevant context. For a context handoff,
it summarizes the current changes, affected files, and anything the other session
needs to know. Replies arrive automatically. Sending a task does not mean the other
session has finished it.

You can also ask to list available sessions or check your inbox. Claude Code can
address Codex sessions through its built-in session messaging.

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

## Automatic reception

Codex sessions appear in Claude Code’s `ListAgents`, receive messages in a durable
inbox, and wake automatically when idle. Python’s standard library handles the local
peer protocol; native `codex queue` handles reception. No launcher or host-specific
integration is required.

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

The plugin authorizes local sending and replies by default. Agents answer within
their existing task and permissions. Incoming messages remain peer data and do not
authorize unrelated actions or tool use. When permission modes differ, messages are
held for explicit approval instead of delivered automatically.

Claude can also use `SendMessage` with `notify_when_idle: true`, with or without a
message. It receives one automatic status notice once Codex finishes its turn with no
pending or held inbox messages. This does not require a model call or polling by Claude.
A status notice is separate from a conversational reply. Subscriptions survive listener
upgrades; a clean session exit sends a best-effort terminal notice. Abrupt process death
cannot guarantee a notice.

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
Subscriptions expire after 12 hours and are limited to 32 peers, one per peer process.
Attachments, remote-host messaging, outgoing idle subscriptions, artifact reply ownership,
and peer lifecycle control are not implemented.

## Troubleshooting

- **Missing from ListAgents:** confirm hook trust, start/resume a Codex thread, then
  check `status`. Both tools must use the same `CLAUDE_CONFIG_DIR`.
- **Message held:** inspect `inbox` and the two sessions’ permission modes; approve
  deliberately with `accept`. Do not invent a mode to bypass parity checks.
- **Stored but not waking:** check `status` → `autoReceive.lastError`, Codex CLI
  compatibility, and matching Codex home/SQLite configuration. Allow a watcher interval.
- **An old prototype is still active:** check `codex plugin list`. Use the released
  `agent-bridge@agent-bridge` plugin, rather than `cross-session-messaging@personal`.
- **Claude thinks a listener restart is a new session:** rediscover by the stable name
  or session UUID. Listener PIDs and sockets change during upgrades; the thread does not.
- **No registered listener:** inspect the hook output or per-thread `daemon.log`
  under `XSM_DATA_DIR`. Do not share runtime keys or inbox files publicly.

## Development

```sh
python3 -m unittest discover -s tests -v
XSM_TEST_CODEX=1 python3 -m unittest discover -s tests -p test_codex_queue.py -v
XSM_TEST_NATIVE=1 python3 -m unittest discover -s tests -p test_claude_native.py -v
```

The standard suite needs no packages or model service. Native tests create isolated
sessions: Codex and Claude idle-notification tests use loopback mock models; Claude's
refusal test does not invoke a model. Tests never message existing user sessions.

To install this checkout locally:

```sh
codex plugin marketplace add .
codex plugin add agent-bridge@agent-bridge
```

See [AGENTS.md](AGENTS.md) for contribution constraints and [the protocol notes](docs/protocol.md)
for the wire format. Licensed under [MIT](LICENSE). This project is not affiliated
with OpenAI or Anthropic.
