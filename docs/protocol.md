# Observed local peer protocol

Verified with Claude Code **2.1.272** on macOS and Codex CLI **0.154.0**.
This is an independently implemented, unofficial interface, not an Anthropic
compatibility promise. No extracted vendor implementation is included.

## Discovery

Claude peers publish `sessions/<PID>.json` beneath `CLAUDE_CONFIG_DIR` (default
`~/.claude`). The bridge publishes a real listener PID, not a temporary hook PID.
Relevant fields:

```json
{
  "pid": 12345,
  "sessionId": "11111111-1111-4111-8111-111111111111",
  "cwd": "/example/project",
  "startedAt": 1789556400000,
  "procStart": "Wed Sep 16 11:00:00 2026",
  "version": "codex-xsm/0.2.0",
  "peerProtocol": 1,
  "peerFeatures": ["reply_across_default_dirs", "notify_idle"],
  "kind": "interactive",
  "entrypoint": "codex",
  "pidDomain": "darwin",
  "messagingSocketPath": "/tmp/cc-socks-501/12345.sock",
  "name": "example-k7",
  "nameSource": "derived",
  "nameSince": 1789556400000,
  "updatedAt": 1789556400000,
  "status": "idle",
  "statusUpdatedAt": 1789556400000
}
```

Times are Unix milliseconds. `procStart` is the trimmed output of
`LC_ALL=C TZ=UTC ps -o lstart= -p <PID>`. Discovery validates liveness and process start
time to reject recycled PIDs. The bridge advertises only implemented features.

## Endpoint identity and authentication

The transport is newline-delimited UTF-8 JSON over an `AF_UNIX` stream socket.
The inspected native implementation uses `/tmp/cc-socks/<PID>.sock`; the bridge uses
the supported per-user fallback `/tmp/cc-socks-<uid>/<PID>.sock`. Paths must fit the
portable 103-byte limit. Do not substitute arbitrary paths for interoperability.

For a lexical absolute socket path `S`, the registry key filename is:

```text
<PID>.<lowercase SHA-256 of UTF-8(S)>.key
```

This is lexical normalization, not symlink resolution. The mode-0600 key file contains
`peerToken` (16 random bytes encoded as 32 hex characters), `procStart`, and `pidDomain`.
Native child-process credentials are distinct from the published peer token.

The first frame authenticates against the recipient’s published key:

```json
{"type":"auth","token":"<recipient peer token>"}
```

The bridge additionally verifies same-user kernel credentials: `LOCAL_PEERPID` plus
`getpeereid` on macOS; `SO_PEERCRED` on Linux. A claimed sender address must resolve to
the verified sender PID. Outbound traffic originates from the listener process whose
PID was registered. Its separate local admin token is never published to peers.

## User message

```json
{
  "msgV": 1,
  "msg_id": "22222222-2222-4222-8222-222222222222",
  "type": "user",
  "priority": "next",
  "session_id": "11111111-1111-4111-8111-111111111111",
  "from": "uds:/tmp/cc-socks-501/23456.sock",
  "message": {
    "role": "user",
    "content": "<cross-session-message from=\"uds:/tmp/cc-socks-501/23456.sock\" from-session=\"33333333-3333-4333-8333-333333333333\" from-name=\"sender-p2\" from-mode=\"prompting\">\nHello\n</cross-session-message>"
  }
}
```

Envelope attributes use the observed order. `from-mode` is `prompting` or `bypass`;
it is not an arbitrary permission string. Body text cannot close the outer envelope.
A missing `session_id` is accepted for native compatibility; a mismatched one is not.

Delivery policy defaults to matching permission classes. Unknown receiver permissions
hold messages. Mismatched modes hold messages pending explicit approval. A receiver
in bypass mode also holds messages with unknown sender mode. Remote metadata never
changes the recipient’s actual permissions.

Stored pending, held, and consumed messages are UUID-deduplicated. Refused and
queue-full messages are not retained, so a repeated UUID can produce another receipt.
The bridge bounds frames, bodies, and pending/held inbox count. It persists accepted
messages before requesting a Codex wake.

## Control receipts

Native receipts arrive on a new authenticated connection to the sender’s socket:

```json
{
  "type": "control",
  "action": "peer_message_status",
  "msgV": 1,
  "msg_id": "44444444-4444-4444-8444-444444444444",
  "orig_msg_id": "22222222-2222-4222-8222-222222222222",
  "status": "held"
}
```

Recognized statuses include `held`, `denied`, `expired`, `delivered`, `refused`, and
`dropped`. The bridge accepts receipts only for known outbound IDs from the verified
target PID. Native `expired` with `status_detail: refused` is preserved and annotated
locally with `normalized_status: refused`. Socket-write success is not model delivery.

## Idle subscriptions

The `notify_idle` capability accepts a control frame with `action: notify_when_idle`,
`msg_id`, `from`, and optional `from_mode`. The reply address must belong to the
kernel-verified registered sender. It records a one-shot subscription without waking
the model. A later authenticated connection carries:

```json
{
  "type": "control",
  "action": "peer_idle_notice",
  "msgV": 1,
  "msg_id": "44444444-4444-4444-8444-444444444444",
  "orig_msg_id": "22222222-2222-4222-8222-222222222222",
  "state": "idle",
  "finished_at": 1789556400000,
  "from": "uds:/tmp/cc-socks-501/12345.sock",
  "from_mode": "prompting"
}
```

States are `idle`, `exited`, or `unavailable`; `finished_at` is omitted for unavailable.
The bridge sends no conversation excerpt in `detail`. An idle transition is debounced
750 ms and requires an empty pending/held inbox. A Stop hook that continues processing
is still busy. Capacity is 32 peers with one subscription per verified PID, refreshed
by a newer request. Expiry is 12 hours, and transient delivery gets one retry.
Clean exit notifications are best effort. Restarting the listener for an upgrade
preserves subscriptions and thread/name timestamps without claiming the thread exited.

## Capability audit (Claude Code 2.1.272)

The native registry advertises three feature names. This is the observed build's
capability set, not a guarantee about future versions.

| Capability or message | Bridge support | Relevance |
| --- | --- | --- |
| `reply_across_default_dirs` | Implemented | Reply across supported local socket directories |
| `notify_idle` | Incoming subscriptions and outgoing notices | Claude can wait for Codex to become idle |
| Outgoing `notify_when_idle` / incoming `peer_idle_notice` | Not implemented | Useful next step for Codex waiting on Claude |
| `artifact_yield` | Not advertised | Coordinates ownership of replies to Claude artifacts, not ordinary chat |
| `yield_artifact_replies`, `artifact_replies_yielded`, `unyield_artifact_replies` | Ignored | Requires a shared artifact/comment system before it is useful |
| `peer_message_status` | Implemented | Held, denied, expired, refused, delivered, dropped |
| `rename` control | Ignored from peers; local rename supported | Remote control of a session name is unnecessary for messaging |
| Hop-chain loop detection, repeated-body suppression, rate limits | UUID deduplication and inbox/frame limits only | Additional guards are useful for autonomous conversations |
| `file_attachments` | Text only; attachment metadata is not materialized | Optional local file transfer, separate from message text |
| Message priority `now`, `next`, `later` | Outgoing `next`; incoming waits for a safe boundary | Interrupting active Codex work is not part of the current workflow |

Native idle notices can include a short final-response `detail`. This is optional;
the bridge deliberately sends status alone and leaves answers to ordinary messages.
The native user-message envelope also carries an optional `hop-chain`. The bridge
parses it syntactically but does not propagate a relay chain. Automatic forwarding is
not part of the plugin workflow. Native ingress additionally uses a 30-message token
bucket replenished at 0.5 messages/second per sender, a 30-second consecutive-body
deduplication window, and hop-chain limits. These are worthwhile follow-up protections
for autonomous conversations; the current bridge does not claim parity with them.

## Codex reception

Lifecycle hooks register a listener and consume pending input at SessionStart,
UserPromptSubmit, PostToolUse, and Stop. Stop can continue the turn once and avoids an
unconditional polling loop. Hook content is wrapped as untrusted peer data.

For idle wake-up, the listener invokes native `codex queue` with a fixed inbox notice.
The peer body stays in the durable inbox. Without a daemon, the CLI uses an embedded
queue writer and exits. It reads the target’s persisted metadata without resuming it.
The already-running Codex process notices the shared queue update and dispatches it.

Codex 0.154.0’s queue extension polls SQLite’s data version every 10 seconds and uses
a durable per-thread revision index. It considers loaded/resumed threads, waits for
active work to finish, and respects interruption. The bridge never edits Codex’s
SQLite database directly. Explicit remote endpoints remain optional.

This behavior was checked against Codex commit
`36eab01061df3cde5f95ec20a526777b430091ba`, in `codex-rs/`:

- `tui/src/session_queue_commands.rs`
- `app-server/src/request_processors/thread_queue_processor.rs`
- `ext/queue/src/service.rs`

The automated native test uses an unmodified stdio app-server without a shared control
socket. Two successive peer messages each wake the same thread and enter a loopback
mock model’s context. Native Claude tests verify an authentic refusal receipt without
starting a model turn, and drive Claude's own `SendMessage` tool through a loopback
mock model to subscribe and then receive an idle notice. No paid model is used.
Live manual testing also established a name-addressed round trip with Claude Code.

Linux native interoperability, attachments, artifact exchange, outgoing idle subscriptions,
and remote-host messaging remain unverified or unimplemented. Recheck this contract
before claiming support for a new native version.
