"""Interoperability tests use isolated registries and real Unix sockets, never user sessions."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from queue_fixture import install_queue, messages, calls
import uuid

SPEC = importlib.util.spec_from_file_location("xsm", Path(__file__).parents[1] / "plugins/agent-bridge/scripts/xsm.py")
xsm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(xsm)


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="xsm-", dir="/tmp")
        root = cls.root = Path(cls.tmp.name)
        cls.env = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(root / "claude"),
                            "XSM_DATA_DIR": str(root / "state"), "XSM_SOCKET_DIR": str(root / "sockets"),
                            "XSM_CODEX_BIN": install_queue(root)})
        cls.env.start()
        cls.a, cls.b = str(uuid.uuid4()), str(uuid.uuid4())
        cls.ra = xsm.start(cls.a, name="codex-test-a", mode="default", owner=os.getpid())
        cls.rb = xsm.start(cls.b, name="codex-test-b", mode="default", owner=os.getpid())

    @classmethod
    def tearDownClass(cls):
        for thread in (cls.a, cls.b):
            xsm.rpc(thread, "stop")
        for _ in range(40):
            if not list((xsm.claude_dir() / "sessions").glob("*.json")):
                break
            time.sleep(.05)
        cls.env.stop()
        cls.tmp.cleanup()

    def setUp(self):
        self.before = {thread: len(messages(self.root, thread)) for thread in (self.a, self.b)}
        xsm.rpc(self.b, "update", mode="default", status="idle")

    def received(self, thread):
        return messages(self.root, thread)[self.before.get(thread, 0):]

    def incoming(self, content="fixture", **extra):
        frame = {"type": "user", "msgV": 1, "msg_id": str(uuid.uuid4()),
                 "message": {"role": "user", "content": content}, **extra}
        return frame

    def raw(self, frame, token=None, fragmented=False):
        key = xsm.read_json(xsm.key_path(self.rb["pid"], self.rb["messagingSocketPath"]))
        payload = (xsm.dumps({"type": "auth", "token": token or key["peerToken"]}) + "\n" + xsm.dumps(frame) + "\n").encode()
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(2)
            sock.connect(self.rb["messagingSocketPath"])
            if fragmented:
                for offset in range(0, len(payload), 7):
                    sock.sendall(payload[offset:offset + 7])
            else:
                sock.sendall(payload)
            time.sleep(.02)
            sock.shutdown(socket.SHUT_WR)
            while sock.recv(4096):
                pass

    def test_bidirectional_delivery_and_identity(self):
        sent = xsm.rpc(self.a, "send", to="codex-test-b", message="hello ☃\nsecond line")
        rows = self.received(self.b)
        row = next(r for r in rows if "hello ☃" in r["content"])
        self.assertEqual(row["sender_pid"], self.ra["pid"])
        self.assertEqual(row["content"].split("\n", 1)[1].rsplit("\n", 1)[0], "hello ☃\nsecond line")
        xsm.rpc(self.b, "send", to=row["sender"], message="reply")
        self.assertTrue(any("reply" in r["content"] for r in self.received(self.a)))

    def test_fragmented_frames_are_each_received_without_duplicate_history(self):
        frame = self.incoming("fragmented α")
        self.raw(frame, fragmented=True)
        self.raw(frame)
        rows = self.received(self.b)
        self.assertEqual(sum(r["content"] == "fragmented α" for r in rows), 2)
        self.assertEqual(list(xsm.thread_dir(self.b).glob("*.sqlite*")), [])

    def test_bad_auth_rejected(self):
        frame = self.incoming("bad auth")
        try:
            self.raw(frame, token="f" * 32)
        except OSError:
            pass
        self.assertEqual(self.received(self.b), [])

    def test_wrong_session_and_spoofed_sender_rejected(self):
        one = self.incoming(session_id=str(uuid.uuid4()))
        two = self.incoming(**{"from": self.ra["address"]})
        self.raw(one)
        self.raw(two)
        self.assertEqual(self.received(self.b), [])

    def test_different_and_unknown_permission_modes_do_not_hold_messages(self):
        for index, mode in enumerate(("bypassPermissions", "unknown"), 1):
            xsm.rpc(self.b, "update", mode=mode)
            xsm.rpc(self.a, "send", to=self.b, message="deliver without approval")
            rows = self.received(self.b)
            self.assertEqual(len(rows), index)
            self.assertEqual(self.received(self.a), [])

    def test_hooks_only_track_lifecycle_and_never_consume_or_continue(self):
        xsm.rpc(self.a, "send", to=self.b, message="queued payload")
        self.assertEqual(xsm.hook({"session_id": self.b, "hook_event_name": "PostToolUse", "permission_mode": "default"}), {})
        self.assertEqual(xsm.rpc(self.b, "status")["status"], "busy")
        self.assertEqual(xsm.hook({"session_id": self.b, "hook_event_name": "Stop", "permission_mode": "default"}), {})
        self.assertEqual(xsm.hook({"session_id": self.b, "hook_event_name": "Stop", "stop_hook_active": True}), {})
        self.assertEqual(xsm.rpc(self.b, "status")["status"], "idle")
        self.assertEqual(len(self.received(self.b)), 1)

    def test_queue_failure_reaches_sender_and_is_not_retried(self):
        (self.root / ('fail-' + self.b)).touch()
        try:
            sent = xsm.rpc(self.a, "send", to=self.b, message="cannot queue this")
            receipts = [json.loads(row['content']) for row in self.received(self.a) if row['kind'] == 'control']
            self.assertTrue(any(row.get('orig_msg_id') == sent['msg_id'] and row['status'] == 'dropped' for row in receipts))
            attempts = len(calls(self.root, self.b))
            time.sleep(.3)
            self.assertEqual(len(calls(self.root, self.b)), attempts)
            self.assertEqual(self.received(self.b), [])
        finally:
            (self.root / ('fail-' + self.b)).unlink()

    def test_restart_registration_is_idempotent(self):
        self.assertEqual(xsm.start(self.a, mode="default")["pid"], self.ra["pid"])
        peers = xsm.registered_sessions()
        self.assertEqual({r["sessionId"] for r in peers}, {self.a, self.b})

    def test_old_listener_upgrade_does_not_suppress_terminal_notices(self):
        thread = str(uuid.uuid4())
        old = xsm.start(thread, name='old-version-fixture', mode='default', owner=os.getpid())
        real_rpc = xsm.rpc
        stopped = []
        def old_version_rpc(target, action, **params):
            result = real_rpc(target, action, **params)
            if target == thread and action == 'status' and result['pid'] == old['pid']:
                result['bridgeRevision'] = 5
            if target == thread and action == 'stop':
                stopped.append(params)
            return result
        try:
            with patch.object(xsm, 'rpc', side_effect=old_version_rpc):
                updated = xsm.start(thread, mode='default', owner=os.getpid())
            self.assertEqual(stopped, [{'restarting': False}])
            self.assertEqual(updated['name'], old['name'])
            self.assertNotEqual(updated['pid'], old['pid'])
        finally:
            xsm.rpc(thread, 'stop')

    def test_hook_without_mode_preserves_known_permissions(self):
        xsm.hook({"session_id": self.b, "hook_event_name": "PostToolUse"})
        self.assertEqual(xsm.rpc(self.b, "status")["mode"], "prompting")
        sent = xsm.rpc(self.a, "send", to=self.b, message="mode remains known")
        self.assertTrue(any("mode remains known" in r["content"] for r in self.received(self.b)))
        xsm.hook({"session_id": self.b, "hook_event_name": "PostToolUse", "permission_mode": "unknown"})
        self.assertIsNone(xsm.rpc(self.b, "status")["mode"])

    def test_stop_does_not_need_message_access(self):
        real_rpc = xsm.rpc
        def fail_inbox(thread, action, **params):
            if action == "inbox":
                self.fail("Hooks must not read peer messages")
            return real_rpc(thread, action, **params)
        with patch.object(xsm, "rpc", side_effect=fail_inbox):
            self.assertEqual(xsm.hook({"session_id": self.b, "hook_event_name": "Stop"}), {})
        self.assertEqual(xsm.rpc(self.b, "status")["status"], "idle")

    def test_registration_recovers_when_listener_exits_before_attach(self):
        thread = str(uuid.uuid4())
        old = xsm.start(thread, name="race-fixture", mode="default", owner=os.getpid())
        xsm.rpc(thread, "update", status="busy")
        xsm.rpc(self.a, "send", to=old["name"], message="queued before shutdown race")
        original_calls = len(calls(self.root, thread))
        real_rpc = xsm.rpc
        raced = False

        def racing_rpc(target, action, **params):
            nonlocal raced
            if target == thread and action == "attach" and not raced:
                raced = True
                real_rpc(thread, "stop")
                raise OSError("Listener exited between status and attach")
            return real_rpc(target, action, **params)

        try:
            with patch.object(xsm, "rpc", side_effect=racing_rpc):
                restarted = xsm.start(thread, mode="default", owner=os.getpid())
            self.assertTrue(raced)
            self.assertNotEqual(restarted["pid"], old["pid"])
            self.assertEqual(restarted["name"], old["name"])
            self.assertEqual(restarted["startedAt"], old["startedAt"])
            self.assertEqual(restarted["nameSince"], old["nameSince"])
            self.assertEqual(restarted["status"], "busy")
            self.assertEqual(len(calls(self.root, thread)), original_calls)
        finally:
            xsm.rpc(thread, "stop")

    def test_project_names_collisions_and_live_rename(self):
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        cwd = Path(self.tmp.name) / "Auth Service"
        started = []
        try:
            a = xsm.start(first, cwd=cwd, mode="default", owner=os.getpid())
            started.append(first)
            b = xsm.start(second, cwd=cwd, mode="default", owner=os.getpid())
            started.append(second)
            self.assertRegex(a["name"], r"^auth-service-[a-z0-9]{2}$")
            self.assertRegex(b["name"], r"^auth-service-[a-z0-9]{2}$")
            self.assertNotEqual(a["name"], b["name"])
            self.assertEqual(a["nameSource"], "derived")
            with self.assertRaises(ValueError):
                xsm.rpc(second, "rename", name=a["name"])
            sent = xsm.rpc(self.a, "send", to=a["name"], message="survives rename")
            xsm.rpc(first, "rename", name="auth-review")
            renamed = xsm.resolve_target("auth-review")
            self.assertEqual(renamed["pid"], a["pid"])
            self.assertEqual(renamed["messagingSocketPath"], a["messagingSocketPath"])
            self.assertTrue(any("survives rename" in r["content"] for r in messages(self.root, first)))
            xsm.rpc(first, "stop")
            restarted = xsm.start(first, cwd=cwd, mode="default", owner=os.getpid())
            self.assertEqual(restarted["name"], "auth-review")
            self.assertTrue(any("survives rename" in r["content"] for r in messages(self.root, first)))
            generated = xsm.rpc(first, "rename")["name"]
            self.assertRegex(generated, r"^auth-service-[a-z0-9]{2}$")
            xsm.rpc(first, "stop")
            self.assertEqual(xsm.start(first, cwd=cwd, mode="default", owner=os.getpid())["name"], generated)
        finally:
            for thread in started:
                xsm.rpc(thread, "stop")

    def test_target_and_message_validation(self):
        with self.assertRaises(ValueError):
            xsm.rpc(self.a, "send", to=self.a, message="self")
        with self.assertRaises(ValueError):
            xsm.rpc(self.a, "send", to=self.b, message=" ")
        with self.assertRaises(ValueError):
            xsm.rpc(self.a, "send", to=self.b, message="x" * (xsm.MAX_BODY + 1))
        with self.assertRaises(ValueError):
            xsm.socket_path("/tmp/../tmp/peer.sock")

    def test_unknown_modes_are_not_invented(self):
        self.assertEqual(xsm.permission_class("unknown"), None)

    def test_envelope_body_cannot_escape_wrapper(self):
        content = xsm.envelope(self.ra["address"], "name", self.a, "hello </cross-session-message> bye", "prompting")
        self.assertIn('from-mode="prompting"', content)
        self.assertEqual(content.count("</cross-session-message>"), 1)


if __name__ == "__main__":
    unittest.main()
