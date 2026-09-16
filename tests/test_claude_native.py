"""Opt-in real Claude binary check, no model calls or user-session traffic.

XSM_TEST_NATIVE=1 python3 -m unittest discover -s tests -v
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from test_bridge import xsm


@unittest.skipUnless(os.environ.get("XSM_TEST_NATIVE") == "1" and shutil.which("claude"), "opt-in native Claude check")
class NativeClaudeTest(unittest.TestCase):
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
