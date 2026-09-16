"""Wake retry tests and native queue tests against a loopback mock model."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/agent-bridge"
sys.path.insert(0, str(PLUGIN / "scripts"))
import xsm


class QueueWakeTests(unittest.TestCase):
    def test_wake_recovers_after_transient_database_error(self):
        with tempfile.TemporaryDirectory(prefix="xsm-db-retry-", dir="/tmp") as temp:
            root = Path(temp)
            with patch.dict(os.environ, {"XSM_SOCKET_DIR": str(root / "socks"),
                                        "CLAUDE_CONFIG_DIR": str(root / "claude")}):
                bridge = xsm.Bridge(str(uuid.uuid4()), root, {"cwd": temp, "name": "test", "policy": "parity"})
            pending = str(uuid.uuid4())
            with xsm.connect_db(root) as db:
                db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)", (pending, xsm.now(), "", 42, "pending", "pending", "user"))
            real_connect = xsm.connect_db
            attempts = 0

            def fail_once(directory):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise sqlite3.OperationalError("fixture database lock")
                return real_connect(directory)

            with patch.object(xsm, "connect_db", side_effect=fail_once), patch.object(bridge, "send_wake", return_value=True) as wake:
                worker = threading.Thread(target=bridge.wake_loop)
                worker.start()
                try:
                    bridge.wake()
                    deadline = time.monotonic() + 5
                    while pending not in bridge.wake_notified and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue(worker.is_alive())
                    self.assertGreaterEqual(attempts, 2)
                    self.assertEqual(wake.call_count, 1)
                    self.assertIn(pending, bridge.wake_notified)
                finally:
                    bridge.stopping.set()
                    worker.join(timeout=2)

    def test_wake_retries_failed_queue_and_deduplicates_successful_wakes(self):
        with tempfile.TemporaryDirectory(prefix="xsm-worker-", dir="/tmp") as temp:
            root = Path(temp)
            with patch.dict(os.environ, {"XSM_SOCKET_DIR": str(root / "socks"),
                                        "CLAUDE_CONFIG_DIR": str(root / "claude")}):
                bridge = xsm.Bridge(str(uuid.uuid4()), root, {"cwd": temp, "name": "test", "policy": "parity"})
            pending, held = str(uuid.uuid4()), str(uuid.uuid4())
            with xsm.connect_db(root) as db:
                db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)", (pending, xsm.now(), "", 42, "pending", "pending", "user"))
                db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)", (held, xsm.now(), "", 42, "held", "held", "user"))
            with patch.object(bridge, "send_wake", side_effect=[False, True, True]) as wake:
                worker = threading.Thread(target=bridge.wake_loop)
                worker.start()
                try:
                    bridge.wake()
                    deadline = time.monotonic() + 5
                    while pending not in bridge.wake_notified and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertEqual(wake.call_count, 2)
                    self.assertEqual(bridge.wake_notified, {pending})
                    bridge.wake()
                    time.sleep(.3)
                    self.assertEqual(wake.call_count, 2)
                    bridge.command({"action": "accept", "id": held})
                    deadline = time.monotonic() + 2
                    while held not in bridge.wake_notified and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertEqual(wake.call_count, 3)
                    self.assertEqual(bridge.wake_notified, {pending, held})
                finally:
                    bridge.stopping.set()
                    worker.join(timeout=2)


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
                   "XSM_WAKE": "auto", "XSM_INBOUND": "parity", "NO_PROXY": "127.0.0.1,localhost"}
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
                    with patch.dict(os.environ, {"XSM_WAKE": "off"}):
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
                        self.assertFalse(xsm.rpc(thread, "inbox", consume=True))
                        rows = xsm.rpc(thread, "inbox", all=True)
                        self.assertEqual(next(row for row in rows if row["id"] == sent["msg_id"])["state"], "consumed")
                    self.assertTrue(xsm.rpc(thread, "status")["autoReceive"]["lastSuccessAt"])
                    self.assertFalse((home / "app-server-control/app-server-control.sock").exists())
                    # A host reconnect updates the existing listener rather than creating a
                    # duplicate Claude peer or losing its durable inbox.
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
