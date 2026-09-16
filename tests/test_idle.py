"""Idle protocol fixtures use isolated registries; no live user-session traffic."""
import json
import os
from pathlib import Path
import queue
import socketserver
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from test_bridge import xsm


class IdleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xsm-idle-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "claude"),
                         "XSM_SOCKET_DIR": str(self.root / "sockets"),
                         "XSM_DATA_DIR": str(self.root / "state"), "XSM_WAKE": "off"})
        env.start()
        self.addCleanup(env.stop)
        self.thread = str(uuid.uuid4())
        self.config = {"cwd": str(self.root), "name": "fixture", "policy": "parity", "mode": "prompting"}
        self.bridge = xsm.Bridge(self.thread, self.root, self.config)
        xsm.private_dir(xsm.claude_dir() / "sessions")
        self.bridge.record["statusUpdatedAt"] = xsm.now() - 1000
        self.target = {"pid": 12345, "address": "uds:/tmp/cc-socks/12345.sock", "procStart": "fixture"}
        self.sent = []
        wire = patch.object(xsm, "wire_send", side_effect=lambda target, frame, **kw: self.sent.append((target, frame)))
        self.wire = wire.start()
        self.addCleanup(wire.stop)

    def subscribe(self, **target):
        request = str(uuid.uuid4())
        self.bridge.subscribe({**self.target, **target}, request)
        return request

    def test_idle_is_one_shot_refreshes_and_has_no_model_wake(self):
        with patch.object(self.bridge, "wake") as wake:
            self.subscribe()
            latest = self.subscribe()
            self.bridge.flush_idle()
            self.bridge.flush_idle()
        wake.assert_not_called()
        self.assertEqual(len(self.sent), 1)
        frame = self.sent[0][1]
        self.assertEqual((frame["action"], frame["state"], frame["orig_msg_id"]),
                         ("peer_idle_notice", "idle", latest))
        self.assertEqual(frame["from_mode"], "prompting")
        self.assertNotIn("detail", frame)

    def test_busy_debounce_pending_and_held_prevent_idle(self):
        self.subscribe()
        self.bridge.command({"action": "update", "status": "busy"})
        self.bridge.flush_idle()
        self.bridge.command({"action": "update", "status": "idle"})
        self.bridge.flush_idle()
        self.assertEqual(self.sent, [])
        self.bridge.record["statusUpdatedAt"] -= 1000
        for state in ("pending", "held"):
            with xsm.connect_db(self.root) as db:
                db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)",
                           (str(uuid.uuid4()), xsm.now(), "", 42, "fixture", state, "user"))
            self.bridge.flush_idle()
            self.assertEqual(self.sent, [])
            with xsm.connect_db(self.root) as db:
                db.execute("UPDATE messages SET state='consumed'")
        self.bridge.flush_idle()
        self.assertEqual(len(self.sent), 1)

    def test_capacity_expiry_and_refusal(self):
        for pid in range(100, 100 + xsm.MAX_SUBSCRIPTIONS):
            self.subscribe(pid=pid)
        overflow = self.subscribe(pid=999)
        self.assertEqual(self.sent[-1][1]["state"], "unavailable")
        self.assertEqual(self.sent[-1][1]["orig_msg_id"], overflow)
        with xsm.connect_db(self.root) as db:
            db.execute("UPDATE subscriptions SET requested=?", (xsm.now() - xsm.SUBSCRIPTION_TTL - 1,))
        self.bridge.flush_idle()
        self.assertEqual(len(self.sent), 1)
        self.bridge.config["policy"] = "refuse"
        self.subscribe()
        with xsm.connect_db(self.root) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM subscriptions").fetchone()[0], 0)

    def test_retry_and_restart_preserve_subscription(self):
        request = self.subscribe()
        self.bridge.restarting = True
        self.bridge.flush_idle(exiting=True)
        self.assertEqual(self.sent, [])
        new = xsm.Bridge(self.thread, self.root, self.config)
        new.record["statusUpdatedAt"] -= 1000
        self.wire.side_effect = OSError("fixture transient")
        new.flush_idle()
        self.wire.side_effect = lambda target, frame, **kw: self.sent.append((target, frame))
        new.flush_idle()
        self.assertEqual(self.sent[0][1]["orig_msg_id"], request)
        new.flush_idle()
        self.assertEqual(len(self.sent), 1)

    def test_busy_exit_sends_terminal_notice(self):
        self.subscribe()
        self.bridge.command({"action": "update", "status": "busy"})
        self.bridge.flush_idle(exiting=True)
        self.assertEqual(self.sent[0][1]["state"], "exited")

    def test_idle_send_is_serialized_with_new_work(self):
        for begin_work in (lambda: self.bridge.command({"action": "update", "status": "busy"}),
                           lambda: self.bridge.receive({"type": "user", "msg_id": str(uuid.uuid4()),
                                                       "message": {"content": "new work"}}, 42)):
            self.bridge.record.update(status="idle", statusUpdatedAt=xsm.now() - 1000)
            self.subscribe()
            entered, release, changed = threading.Event(), threading.Event(), threading.Event()
            def send(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(2))
                self.assertFalse(changed.is_set())
            self.wire.side_effect = send
            notifier = threading.Thread(target=self.bridge.flush_idle)
            mutation = threading.Thread(target=lambda: (begin_work(), changed.set()))
            notifier.start()
            try:
                self.assertTrue(entered.wait(2))
                mutation.start()
                self.assertFalse(changed.wait(.1))
            finally:
                release.set()
                notifier.join(timeout=2)
                mutation.join(timeout=2)
            self.assertTrue(changed.is_set())

    def test_exit_claims_rows_before_sending_and_does_not_replay(self):
        self.subscribe()
        entered, release = threading.Event(), threading.Event()
        def send(*args, **kwargs):
            entered.set()
            release.wait(2)
        self.wire.side_effect = send
        exiting = threading.Thread(target=lambda: self.bridge.flush_idle(exiting=True))
        exiting.start()
        try:
            self.assertTrue(entered.wait(2))
            with xsm.connect_db(self.root) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM subscriptions").fetchone()[0], 0)
            self.bridge.flush_idle(exiting=True)
            self.assertEqual(self.wire.call_count, 1)
        finally:
            release.set()
            exiting.join(timeout=2)

    def test_subscription_requires_verified_sender_and_recipient(self):
        request = {"type": "control", "action": "notify_when_idle", "msgV": 1,
                   "msg_id": str(uuid.uuid4()), "from": self.target["address"]}
        with patch.object(xsm, "resolve_target", return_value=self.target):
            self.bridge.receive(request, self.target["pid"] + 1)
            self.bridge.receive({k: v for k, v in request.items() if k != "msg_id"}, self.target["pid"])
            self.bridge.receive({**request, "session_id": str(uuid.uuid4())}, self.target["pid"])
            self.bridge.flush_idle()
            self.assertEqual(self.sent, [])
            self.bridge.receive(request, self.target["pid"])
            self.bridge.flush_idle()
        self.assertEqual(len(self.sent), 1)

    def test_late_receive_cannot_admit_work_after_stop(self):
        self.subscribe()
        entered, release = threading.Event(), threading.Event()
        parse = xsm.parse_envelope
        def delayed_parse(content):
            entered.set()
            release.wait(2)
            return parse(content)
        with patch.object(xsm, "parse_envelope", side_effect=delayed_parse):
            receiver = threading.Thread(target=lambda: self.bridge.receive(
                {"type": "user", "message": {"content": "late work"}, "msg_id": str(uuid.uuid4())}, 42))
            receiver.start()
            try:
                self.assertTrue(entered.wait(2))
                self.bridge.command({"action": "stop"})
                self.bridge.flush_idle(exiting=True)
            finally:
                release.set()
                receiver.join(timeout=2)
        with xsm.connect_db(self.root) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_cross_peer_uuid_collision_and_revoked_policy(self):
        original = self.subscribe()
        self.bridge.subscribe({**self.target, "pid": self.target["pid"] + 1}, original)
        self.assertEqual(self.sent[0][1]["state"], "unavailable")
        with xsm.connect_db(self.root) as db:
            self.assertEqual(db.execute("SELECT target_pid FROM subscriptions").fetchone()[0], self.target["pid"])
        self.bridge.config["policy"] = "refuse"
        self.bridge.flush_idle(exiting=True)
        self.bridge.config["policy"] = "parity"
        self.bridge.flush_idle()
        self.assertEqual(len(self.sent), 1)


class SocketIdleTest(unittest.TestCase):
    def test_authenticated_subscription_and_notice_across_processes(self):
        with tempfile.TemporaryDirectory(prefix="xsm-idle-wire-", dir="/tmp") as temp:
            root = Path(temp)
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(root / "claude"),
                    "XSM_DATA_DIR": str(root / "data"), "XSM_SOCKET_DIR": str(root / "socks"),
                    "XSM_WAKE": "off", "XSM_INBOUND": "parity"}):
                thread = str(uuid.uuid4())
                receiver = xsm.start(thread, name="fixture-codex", mode="default", owner=os.getpid())
                frames = queue.Queue()
                token = "1" * 32
                peer_path = root / "socks" / f"{os.getpid()}.sock"
                record = {"pid": os.getpid(), "sessionId": str(uuid.uuid4()), "peerProtocol": 1,
                          "procStart": xsm.start_token(os.getpid()), "messagingSocketPath": str(peer_path)}
                registry = xsm.claude_dir() / "sessions" / f"{os.getpid()}.json"
                key = xsm.key_path(os.getpid(), peer_path)
                trickle = False
                class Handler(socketserver.StreamRequestHandler):
                    def handle(handler):
                        pid, uid = xsm.peer_identity(handler.request)
                        auth = json.loads(handler.rfile.readline())
                        frame = json.loads(handler.rfile.readline())
                        frames.put((pid, uid, auth, frame))
                        while handler.rfile.readline():
                            pass
                        if trickle:
                            try:
                                for _ in range(40):
                                    handler.wfile.write(b"x")
                                    time.sleep(.05)
                            except OSError:
                                pass
                with socketserver.ThreadingUnixStreamServer(str(peer_path), Handler) as server:
                    worker = threading.Thread(target=server.serve_forever, daemon=True)
                    worker.start()
                    xsm.atomic_json(registry, record)
                    xsm.atomic_json(key, {"peerToken": token, "procStart": record["procStart"]})
                    request = str(uuid.uuid4())
                    try:
                        self.assertIn("notify_idle", receiver["peerFeatures"])
                        xsm.rpc(thread, "update", status="busy")
                        xsm.wire_send(receiver, {"type": "control", "action": "notify_when_idle", "msgV": 1,
                                                "msg_id": request, "from": xsm.address(peer_path), "from_mode": "prompting"})
                        with self.assertRaises(queue.Empty):
                            frames.get(timeout=.9)
                        xsm.rpc(thread, "update", status="idle")
                        pid, uid, auth, notice = frames.get(timeout=4)
                        self.assertEqual((pid, uid), (receiver["pid"], os.getuid()))
                        self.assertEqual(auth, {"type": "auth", "token": token})
                        self.assertEqual((notice["orig_msg_id"], notice["state"]), (request, "idle"))
                        with self.assertRaises(queue.Empty):
                            frames.get(timeout=.8)
                        trickle = True
                        before = time.monotonic()
                        with self.assertRaises(TimeoutError):
                            xsm.wire_send(record, {"type": "control", "action": "fixture"}, timeout=.4)
                        self.assertLess(time.monotonic() - before, 1.2)
                    finally:
                        xsm.rpc(thread, "stop")
                        server.shutdown()
                        worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
