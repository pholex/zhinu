"""可靠消息在日志、回执和重投边界的故障回归。"""
import json
import io
from contextlib import redirect_stdout
from unittest.mock import patch

from xiaoyu import peer_delivery, peers
from xiaoyu.session_log import SessionLog, load_messages
from .test_agent_paths import AgentTestCase, chunk


class ReliableMessages(AgentTestCase):
    def setUp(self):
        super().setUp()
        p = patch.object(peers, "peers_dir", return_value=self.root / "peers")
        p.start()
        self.addCleanup(p.stop)
        self.reg = peers.Registration.create(str(self.root), "test")
        self.addCleanup(self.reg.close)
        self.log = SessionLog(self.root / "session.jsonl")
        self.addCleanup(self.log.release)
        self.agent = self.build([[chunk(content="收到")]], peer=self.reg, session_log=self.log)

    def test_cli_send_status_and_retry_share_receipt(self):
        from xiaoyu.cli import send_command
        def command(args):
            output = io.StringIO()
            with patch("xiaoyu.cli.load_dotenv"), patch("xiaoyu.cli.read_piped_stdin", return_value=""), redirect_stdout(output):
                self.assertEqual(send_command(args), 0)
            return json.loads(output.getvalue())
        sent = command(["--reliable", self.reg.name, "cli report"])
        self.assertEqual(sent["status"], "pending")
        self.assertEqual(command(["--retry", sent["id"], self.reg.name])["id"], sent["id"])
        self.assertTrue(self.agent._consume_inbox())
        self.assertEqual(command(["--status", sent["id"]])["status"], "accepted")

    def test_consumption_is_persisted_and_not_repeated(self):
        record = peer_delivery.send(self.reg.name, "测试通过了", "worker")
        self.assertEqual(peer_delivery.status(record["id"])["status"], "pending")
        self.assertIn("可靠", self.agent._wait_for_event(0))
        self.assertTrue(self.agent._consume_inbox())
        self.assertFalse(self.agent._consume_inbox())
        self.assertEqual(peer_delivery.status(record["id"])["status"], "accepted")
        loaded = load_messages(self.log.path)
        self.assertEqual(sum(m.get("_peer_delivery_id") == record["id"] for m in loaded), 1)
        self.assertEqual(peer_delivery.retry(record["id"], "not-a-session")["status"], "accepted")

    def test_failed_journal_keeps_message_out_of_context(self):
        record = peer_delivery.send(self.reg.name, "report")
        with patch.object(self.log, "commit_event", side_effect=OSError("disk full")):
            self.assertFalse(self.agent._consume_inbox())
        self.assertFalse(any(m.get("_peer_delivery_id") for m in self.agent.messages))
        self.assertTrue((peer_delivery.directory() / f"{record['id']}.json").exists())

    def test_lost_ack_reconciles_from_journal_even_after_compaction(self):
        record = peer_delivery.send(self.reg.name, "report")
        original = peer_delivery._save
        def save(value):
            if value["status"] == "accepted":
                raise OSError("receipt unavailable")
            original(value)
        with patch.object(peer_delivery, "_save", side_effect=save):
            self.assertTrue(self.agent._consume_inbox())
        self.log.event("compact", replacement=[])
        self.assertEqual(peer_delivery.status(record["id"])["status"], "accepted")
        self.assertFalse(self.agent._consume_inbox())
        self.assertEqual(sum(bool(m.get("_peer_delivery_id")) for m in self.agent.messages), 1)

    def test_retry_is_serialized_against_acceptance(self):
        record = peer_delivery.send(self.reg.name, "report")
        with peer_delivery._locked(record["id"]):
            with self.assertRaises(OSError):
                peer_delivery.retry(record["id"], self.reg.name)
        self.assertEqual(peer_delivery.retry(record["id"], self.reg.name)["id"], record["id"])
        self.agent._consume_inbox()
        self.assertEqual(peer_delivery.retry(record["id"], self.reg.name)["status"], "accepted")

    def test_closed_peer_does_not_delete_pending_receipt(self):
        record = peer_delivery.send(self.reg.name, "report")
        self.reg.close()
        self.assertEqual(peer_delivery.status(record["id"])["status"], "pending")
        replacement = peers.Registration.create(str(self.root), "test")
        self.addCleanup(replacement.close)
        replacement.bind_delivery_log(str(self.log.path))
        self.agent.peer = replacement
        self.assertTrue(self.agent._consume_inbox())

    def test_missing_journal_after_attempt_is_uncertain(self):
        record = peer_delivery.send(self.reg.name, "report")
        record["attempted"] = True
        peer_delivery._save(record)
        self.log.release()
        self.log.path.unlink(missing_ok=True)
        with self.assertRaisesRegex(OSError, "无法确定"):
            peer_delivery.retry(record["id"], self.reg.name)

    def test_one_bad_record_does_not_block_healthy_delivery(self):
        record = peer_delivery.send(self.reg.name, "report")
        (peer_delivery.directory() / "broken.json").write_text("bad", encoding="utf-8")
        with self.assertLogs("xiaoyu.peer_delivery", "WARNING"):
            self.assertTrue(self.agent._consume_inbox())
        self.assertEqual(peer_delivery.status(record["id"])["status"], "accepted")
