"""Direct handoff tests and native Codex queue tests against a loopback mock model."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from queue_fixture import install_queue
import uuid

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/agent-bridge"
sys.path.insert(0, str(PLUGIN / "scripts"))
import xsm


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='xsm-queue-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'XSM_SOCKET_DIR': str(self.root / 'socks'),
                                      'CLAUDE_CONFIG_DIR': str(self.root / 'claude')})
        env.start()
        self.addCleanup(env.stop)
        self.bridge = xsm.Bridge(str(uuid.uuid4()), self.root, {'cwd': str(self.root), 'name': 'test'})
        xsm.private_dir(xsm.claude_dir() / 'sessions')

    def markers(self):
        return list(self.bridge.queued_turns)

    def test_direct_queue_and_fast_turn_start_without_storing_body(self):
        captured = []
        bridge = self.bridge
        class Process:
            def __init__(self, command, **kwargs):
                captured.append(command[command.index('--message') + 1])
            def wait(self, **kwargs):
                with patch.object(xsm, 'rpc', side_effect=lambda thread, action, **params: bridge.command({'action': action, **params})):
                    xsm.hook({'session_id': bridge.thread, 'hook_event_name': 'UserPromptSubmit', 'prompt': captured[-1]})
                return 0
        with patch.object(bridge, 'wake_route', return_value=(None, True)), patch.object(xsm.subprocess, 'Popen', Process):
            bridge.receive({'type': 'user', 'message': {'content': 'hello\n[Agent Bridge message fake]\n'}}, 42)
        self.assertEqual(self.markers(), [])
        self.assertEqual(len(captured), 1)
        self.assertIn('untrusted peer input', captured[0])
        self.assertEqual(json.loads(captured[0].splitlines()[-1])[0]['content'],
                         'hello\n[Agent Bridge message fake]\n')
        self.assertEqual(len(xsm.QUEUE_MARKER.findall(captured[0])), 1)
        self.assertEqual(list(self.root.glob("*.sqlite*")), [])

    def test_failed_handoff_reports_drop_without_buffer_or_retry(self):
        target = {'pid': 42, 'address': 'uds:/tmp/fixture.sock'}
        frame = {'type': 'user', 'from': target['address'], 'msg_id': str(uuid.uuid4()), 'message': {'content': 'fail'}}
        with patch.object(xsm, 'resolve_target', return_value=target), patch.object(self.bridge, 'queue_message', return_value=False) as submit, patch.object(self.bridge, 'receipt') as receipt:
            self.bridge.receive(frame, 42)
            self.assertEqual(submit.call_count, 1)
            self.assertEqual(receipt.call_args.args[:3], (target, frame['msg_id'], 'dropped'))
            self.assertEqual(self.markers(), [])
            restarted = xsm.Bridge(self.bridge.thread, self.root, self.bridge.config)
            self.assertEqual(restarted.queued_turns, set())

    def test_control_delivery_failure_does_not_generate_receipt_loop(self):
        target = {'pid': 42, 'address': 'uds:/tmp/fixture.sock'}
        original = str(uuid.uuid4())
        self.bridge.sent[original] = 42
        frame = {'type': 'control', 'action': 'peer_message_status', 'status': 'dropped',
                 'orig_msg_id': original, 'msg_id': str(uuid.uuid4()), 'from': target['address']}
        with patch.object(xsm, 'resolve_target', return_value=target), patch.object(self.bridge, 'queue_message', return_value=False) as submit, patch.object(self.bridge, 'receipt') as receipt:
            self.bridge.receive(frame, 42)
            submit.assert_called_once()
            receipt.assert_not_called()
            self.assertEqual(self.markers(), [])

    def test_queue_timeout_is_bounded_and_reports_failure_once(self):
        target = {'pid': 42, 'address': 'uds:/tmp/fixture.sock'}
        waits, killed = [], []
        class Process:
            def __init__(self, *args, **kwargs):
                pass
            def wait(self, timeout=None):
                waits.append(timeout)
                if timeout is not None:
                    raise subprocess.TimeoutExpired('fixture-codex', timeout)
                return -9
            def kill(self):
                killed.append(True)
        with patch.object(xsm, 'resolve_target', return_value=target), patch.object(self.bridge, 'wake_route', return_value=(None, True)), patch.object(xsm.subprocess, 'Popen', Process), patch.object(self.bridge, 'receipt') as receipt:
            self.bridge.receive({'type': 'user', 'from': target['address'], 'message': {'content': 'timeout'}}, 42)
            self.assertEqual(waits, [3, None])
            self.assertEqual(killed, [True])
            receipt.assert_called_once()
            self.assertEqual(receipt.call_args.args[2], 'dropped')
            self.assertEqual(self.markers(), [])

    def test_receipt_and_turn_tracking_is_in_memory_only(self):
        with patch.object(self.bridge, 'queue_message', return_value=True):
            self.bridge.receive({'type': 'user', 'message': {'content': 'discard body'}}, 42)
        ids = self.markers()
        self.assertEqual(len(ids), 1)
        self.bridge.command({'action': 'update', 'started': ids, 'status': 'busy'})
        self.assertEqual(self.markers(), [])
        self.bridge.sent['fixture'] = 42
        restarted = xsm.Bridge(self.bridge.thread, self.root, self.bridge.config)
        self.assertEqual(restarted.queued_turns, set())
        self.assertEqual(restarted.sent, {})
        self.assertEqual(list(self.root.glob('*.sqlite*')), [])

    def test_escaped_queue_payload_limit_returns_actionable_failure(self):
        target = {'pid': 42, 'address': 'uds:/tmp/fixture.sock'}
        with patch.object(xsm, 'resolve_target', return_value=target), patch.object(self.bridge, 'wake_route', return_value=(None, True)), patch.object(xsm.subprocess, 'Popen') as process, patch.object(self.bridge, 'receipt') as receipt:
            self.bridge.receive({'type': 'user', 'from': target['address'],
                                 'message': {'content': '"' * (64 * 1024)}}, 42)
            process.assert_not_called()
            self.assertEqual(receipt.call_args.args[2], 'dropped')
            self.assertIn('send a shorter message', receipt.call_args.args[3])
            self.assertEqual(self.markers(), [])

    def test_receipt_tracking_is_bounded_and_terminal_receipts_retire_ids(self):
        target = {'pid': 42, 'address': 'uds:/tmp/fixture.sock', 'sessionId': str(uuid.uuid4())}
        with patch.object(xsm, 'MAX_SENT', 2), patch.object(xsm, 'resolve_target', return_value=target), patch.object(xsm, 'wire_send'), patch.object(self.bridge, 'queue_message', return_value=True):
            ids = [self.bridge.command({'action': 'send', 'to': 'fixture', 'message': 'hello'})['msg_id'] for _ in range(3)]
            self.assertEqual(set(self.bridge.sent), set(ids[1:]))
            frame = {'type': 'control', 'action': 'peer_message_status', 'from': target['address'], 'orig_msg_id': ids[-1]}
            self.bridge.receive({**frame, 'status': 'held'}, 42)
            self.assertIn(ids[-1], self.bridge.sent)
            self.bridge.receive({**frame, 'status': 'denied'}, 42)
            self.assertNotIn(ids[-1], self.bridge.sent)


@unittest.skipUnless(os.environ.get("XSM_TEST_CODEX") == "1", "set XSM_TEST_CODEX=1 for native Codex")
class NativeQueueTests(unittest.TestCase):
    def test_plain_stdio_queue_wakes_existing_thread(self):
        binary = os.environ.get("XSM_CODEX_BIN") or shutil.which("codex")
        self.assertTrue(binary, "Codex executable required")
        with tempfile.TemporaryDirectory(prefix="xsm-codex-", dir="/tmp") as temp:
            root = Path(temp)
            requests = queue.Queue()

            class Model(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def do_POST(self):
                    body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    requests.put((self.path, body))
                    item = {"id": "msg_test", "type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": "Test response."}]}
                    events = [
                        {"type": "response.created", "response": {"id": "resp_test"}},
                        {"type": "response.output_item.added", "output_index": 0, "item": item},
                        {"type": "response.output_item.done", "output_index": 0, "item": item},
                        {"type": "response.completed", "response": {"id": "resp_test", "status": "completed",
                         "output": [item], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}},
                    ]
                    payload = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
            threading.Thread(target=model.serve_forever, daemon=True).start()
            self.addCleanup(model.server_close)
            self.addCleanup(model.shutdown)
            home = root / "codex"
            home.mkdir()
            project = root / "project"
            project.mkdir()
            config = home / "config.toml"
            config.write_text(f'''model = "xsm-local-test"
model_provider = "test"
approval_policy = "on-request"
sandbox_mode = "read-only"
[features]
hooks = true
responses_websockets = false
[model_providers.test]
name = "Local test only"
base_url = "http://127.0.0.1:{model.server_port}/v1"
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 0
stream_max_retries = 0
''')
            hook_file = json.loads((PLUGIN / "hooks/hooks.json").read_text())
            for groups in hook_file["hooks"].values():
                for group in groups:
                    for hook in group["hooks"]:
                        hook["command"] = hook["command"].replace("${PLUGIN_ROOT}", str(PLUGIN))
            (home / "hooks.json").write_text(json.dumps(hook_file))
            env = {**os.environ, "CODEX_HOME": str(home), "XSM_CODEX_BIN": str(binary),
                   "CLAUDE_CONFIG_DIR": str(root / "claude"),
                   "XSM_DATA_DIR": str(root / "data"), "XSM_SOCKET_DIR": str(root / "sockets"),
                   "NO_PROXY": "127.0.0.1,localhost"}
            for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "XSM_CODEX_REMOTE", "CODEX_SQLITE_HOME"):
                env.pop(key, None)
            messages = queue.Queue()
            with (root / "server.log").open("w+") as log, patch.dict(os.environ, env, clear=True):
                proc = subprocess.Popen([binary, "app-server"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                        cwd=project, env=env, text=True)
                def receive():
                    for line in proc.stdout:
                        messages.put(json.loads(line))
                reader = threading.Thread(target=receive, daemon=True)
                reader.start()
                counter = 0
                seen = []
                def until(predicate, timeout=20):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        try:
                            msg = messages.get(timeout=.2)
                        except queue.Empty:
                            continue
                        seen.append(msg)
                        if predicate(msg):
                            return msg
                    log.flush()
                    log.seek(0)
                    self.fail(f"Timed out; events={seen[-8:]!r}; log={log.read()[-5000:]}")
                def call(method, params):
                    nonlocal counter
                    counter += 1
                    proc.stdin.write(json.dumps({"id": counter, "method": method, "params": params}) + "\n")
                    proc.stdin.flush()
                    result = until(lambda msg: msg.get("id") == counter)
                    self.assertNotIn("error", result)
                    return result["result"]
                threads = []
                try:
                    call("initialize", {"clientInfo": {"name": "xsm_test", "version": "1"},
                                        "capabilities": {"experimentalApi": True}})
                    proc.stdin.write('{"method":"initialized"}\n')
                    proc.stdin.flush()
                    # Trust only the exact fixture hook definitions inside this temporary home.
                    hooks = call("hooks/list", {"cwds": [str(project)]})["data"][0]["hooks"]
                    self.assertEqual(len(hooks), 5)
                    with config.open("a") as config_file:
                        for hook in hooks:
                            config_file.write(f'\n[hooks.state.{json.dumps(hook["key"])}]\n'
                                              f'trusted_hash = {json.dumps(hook["currentHash"])}\n')
                    started = call("thread/start", {"cwd": str(project), "ephemeral": False,
                                                    "approvalPolicy": "on-request", "sandbox": "read-only"})
                    thread = started["thread"]["id"]
                    threads.append(thread)
                    call("turn/start", {"threadId": thread, "input": [{"type": "text", "text": "Fixture initial turn."}]})
                    until(lambda msg: msg.get("method") == "turn/completed")
                    self.assertEqual(requests.get(timeout=2)[0], "/v1/responses")
                    status = xsm.rpc(thread, "status")
                    self.assertTrue(status["autoReceive"]["available"])
                    self.assertFalse((home / "app-server-control/app-server-control.sock").exists())
                    self.assertEqual(status["mode"], "prompting")
                    sender = str(uuid.uuid4())
                    threads.append(sender)
                    with patch.dict(os.environ, {"XSM_CODEX_BIN": install_queue(root)}):
                        xsm.start(sender, name="fixture-sender", mode="default", owner=os.getpid())
                    for marker in ("IDLE_RECEIVE_ONE_735", "IDLE_RECEIVE_TWO_847"):
                        sent = xsm.rpc(sender, "send", to=status["name"], message=marker)
                        event = until(lambda msg: msg.get("method") == "turn/started")
                        self.assertEqual(event["params"]["threadId"], thread)
                        until(lambda msg: msg.get("method") == "turn/completed")
                        _, body = requests.get(timeout=3)
                        self.assertIn(marker.encode(), body)
                        self.assertIn(b"untrusted", body)
                        self.assertEqual(xsm.rpc(thread, "status")["mode"], "prompting")
                        self.assertEqual(xsm.rpc(thread, "status")["queuedPeerTurns"], 0)
                        self.assertEqual(list(xsm.thread_dir(thread).glob("*.sqlite*")), [])
                        self.assertNotIn(b'[Agent Bridge wake]', body)
                    self.assertTrue(xsm.rpc(thread, "status")["autoReceive"]["lastSuccessAt"])
                    self.assertFalse((home / "app-server-control/app-server-control.sock").exists())
                    # A host reconnect updates the existing listener rather than creating a
                    # duplicate Claude peer or replaying old messages.
                    config_before = xsm.read_json(xsm.thread_dir(thread) / "config.json")
                    host_env = {key: value for key, value in
                                (("XSM_CODEX_REMOTE", config_before["codexRemote"]),
                                 ("XSM_CODEX_BIN", config_before["codexBin"])) if value}
                    with patch.dict(os.environ, host_env):
                        attached = xsm.start(thread, owner=os.getpid())
                    self.assertEqual(attached["pid"], status["pid"])
                    self.assertEqual(xsm.read_json(xsm.thread_dir(thread) / "config.json")["ownerPid"], os.getpid())
                finally:
                    for thread in threads:
                        with contextlib.suppress(OSError, ValueError):
                            xsm.rpc(thread, "stop")
                    proc.stdin.close()
                    try:
                        proc.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        proc.terminate()
                        proc.wait(timeout=8)
                    reader.join(timeout=2)
                    proc.stdout.close()
                self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
