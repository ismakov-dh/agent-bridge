"""Opt-in real Claude binary check, no model calls or user-session traffic.

XSM_TEST_NATIVE=1 python3 -m unittest discover -s tests -v
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import queue
import tempfile
import time
import threading
import unittest
from unittest.mock import patch
import uuid

from test_bridge import xsm


@unittest.skipUnless(os.environ.get("XSM_TEST_NATIVE") == "1" and shutil.which("claude"), "opt-in native Claude check")
class NativeClaudeTest(unittest.TestCase):
    def test_native_idle_subscription_and_notice_with_mock_model(self):
        with tempfile.TemporaryDirectory(prefix="xsm-native-idle-", dir="/tmp") as temp:
            root = Path(temp)
            calls = queue.Queue()
            subscribed = threading.Event()
            counter = 0
            class Model(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def do_POST(self):
                    nonlocal counter
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    if not self.path.startswith("/v1/messages"):
                        self.send_error(404)
                        return
                    counter += 1
                    calls.put(body)
                    if counter == 1:
                        block = {"type": "tool_use", "id": "toolu_idle_fixture", "name": "SendMessage",
                                 "input": {"to": "codex-idle-fixture", "notify_when_idle": True}}
                        reason = "tool_use"
                    else:
                        block = {"type": "text", "text": "Fixture complete."}
                        reason = "end_turn"
                        subscribed.set()
                    result = {"id": f"msg_fixture_{counter}", "type": "message", "role": "assistant",
                              "model": body.get("model"), "content": [block], "stop_reason": reason,
                              "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}
                    if body.get("stream"):
                        message = {**result, "content": [], "stop_reason": None}
                        initial = {**block, "input": {}} if block["type"] == "tool_use" else {"type": "text", "text": ""}
                        delta = ({"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
                                 if block["type"] == "tool_use" else {"type": "text_delta", "text": block["text"]})
                        events = [("message_start", {"message": message}),
                                  ("content_block_start", {"index": 0, "content_block": initial}),
                                  ("content_block_delta", {"index": 0, "delta": delta}),
                                  ("content_block_stop", {"index": 0}),
                                  ("message_delta", {"delta": {"stop_reason": reason, "stop_sequence": None}, "usage": {"output_tokens": 1}}),
                                  ("message_stop", {})]
                        payload = "".join(f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n" for kind, data in events).encode()
                        content_type = "text/event-stream"
                    else:
                        payload = json.dumps(result).encode()
                        content_type = "application/json"
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
            threading.Thread(target=model.serve_forever, daemon=True).start()
            self.addCleanup(model.server_close)
            self.addCleanup(model.shutdown)
            # A closed environment prevents real providers/credentials from being selected.
            env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")}
            env.update(CLAUDE_CONFIG_DIR=str(root / "claude"), XSM_DATA_DIR=str(root / "data"),
                       XSM_WAKE="off", XSM_INBOUND="parity", ANTHROPIC_API_KEY="fixture-only",
                       ANTHROPIC_BASE_URL=f"http://127.0.0.1:{model.server_port}",
                       CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", NO_PROXY="127.0.0.1,localhost")
            with patch.dict(os.environ, env, clear=True):
                own, native = str(uuid.uuid4()), str(uuid.uuid4())
                xsm.start(own, name="codex-idle-fixture", mode="default", owner=os.getpid())
                xsm.rpc(own, "update", status="busy")
                with (root / "out.log").open("w") as out, (root / "err.log").open("w") as err:
                    child = subprocess.Popen([
                        "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                        "--verbose", "--no-session-persistence", "--session-id", native,
                        "--setting-sources", "", "--settings", '{"crossSessionInbound":"accept"}',
                        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                        "--tools", "SendMessage", "--allowedTools", "SendMessage",
                    ], stdin=subprocess.PIPE, stdout=out, stderr=err, text=True, cwd=temp, env=env)
                    try:
                        child.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": "Run the idle subscription fixture."}}) + "\n")
                        child.stdin.flush()
                        self.assertTrue(subscribed.wait(25), (root / "err.log").read_text()[-1500:])
                        calls.get(timeout=2)
                        after_tool = calls.get(timeout=2)
                        self.assertIn("Subscribed", json.dumps(after_tool))
                        xsm.rpc(own, "update", status="idle")
                        notice_turn = calls.get(timeout=20)
                        self.assertIn("Cross-session idle notice", json.dumps(notice_turn))
                        self.assertIn("codex-idle-fixture", json.dumps(notice_turn))
                    finally:
                        child.terminate()
                        try:
                            child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait()
                        child.stdin.close()
                        xsm.rpc(own, "stop")

    def test_native_receiver_and_native_control_reply(self):
        with tempfile.TemporaryDirectory(prefix="xsm-native-", dir="/tmp") as temp:
            root = Path(temp)
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(root / "claude"),
                    "XSM_DATA_DIR": str(root / "state"), "XSM_SOCKET_DIR": str(root / "sockets"),
                    "XSM_WAKE": "off", "XSM_INBOUND": "parity"}):
                # Use the default socket namespace for reply-address vetting across processes.
                os.environ.pop("XSM_SOCKET_DIR", None)
                own, native = str(uuid.uuid4()), str(uuid.uuid4())
                xsm.start(own, name="codex-native-test", mode="default", owner=os.getpid())
                with (root / "out.log").open("w") as out, (root / "err.log").open("w") as err:
                    child = subprocess.Popen([
                        "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                        "--verbose", "--no-session-persistence", "--session-id", native,
                        "--setting-sources", "", "--settings", '{"crossSessionInbound":"refuse"}',
                        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--tools", "",
                    ], stdin=subprocess.PIPE, stdout=out, stderr=err, text=True, cwd=temp)
                    try:
                        child.stdin.write(json.dumps({"type": "control_request", "request_id": str(uuid.uuid4()),
                                                      "request": {"subtype": "initialize"}}) + "\n")
                        child.stdin.flush()
                        target = None
                        for _ in range(80):
                            if child.poll() is not None:
                                self.fail("Claude exited before publishing its socket: " + (root / "err.log").read_text()[:500])
                            try:
                                target = xsm.resolve_target(native)
                                break
                            except ValueError:
                                time.sleep(.1)
                        self.assertIsNotNone(target, "Claude did not publish a live peer socket")
                        sent = xsm.rpc(own, "send", to=native, message="xsm protocol test: refuse without invoking a model")
                        receipt = None
                        for _ in range(40):
                            for row in xsm.rpc(own, "inbox"):
                                if row["kind"] == "control":
                                    value = json.loads(row["content"])
                                    if value.get("orig_msg_id") == sent["msg_id"]:
                                        receipt = value
                                        break
                            if receipt:
                                break
                            time.sleep(.1)
                        self.assertIsNotNone(receipt, "No native control receipt for the bridge message")
                        self.assertEqual(receipt.get("normalized_status", receipt["status"]), "refused")
                        out.flush()
                        # The refusal path must not start an assistant/model turn.
                        self.assertNotIn('"type":"assistant"', (root / "out.log").read_text())
                    finally:
                        child.terminate()
                        try:
                            child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait()
                        child.stdin.close()
                        xsm.rpc(own, "stop")


if __name__ == "__main__":
    unittest.main()
