import tempfile
import unittest
from pathlib import Path

from event_pipeline import Inbox


def msg(n, outgoing=False, text="hello", stamp=100):
    return dict(local_id=n, outgoing=outgoing, text=text, timestamp=stamp, type=1, sender="friend")


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "state.db")
        self.db = Inbox(self.path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def ingest(self, user, items, priority=4):
        return self.db.ingest("account", user, "message/message_0.db", user, items, priority)

    def test_restart_resumes_cursor_and_does_not_duplicate(self):
        self.ingest("friend", [msg(1), msg(2, True)])
        self.db.close()
        self.db = Inbox(self.path)
        self.assertEqual(self.db.cursor("account", "friend", "message/message_0.db"), 2)
        self.ingest("friend", [msg(1), msg(2, True), msg(3)])
        self.assertEqual(self.db.count(), 2)

    def test_new_private_overtakes_old_group_but_chat_order_is_preserved(self):
        self.ingest("group@chatroom", [msg(1, stamp=1)], 1)
        self.ingest("friend", [msg(2, stamp=3), msg(3, stamp=4)])
        claimed = self.db.claim(set(), now=200)
        self.assertEqual(claimed[0]["username"], "friend")
        self.assertEqual([r["message"]["local_id"] for r in claimed], [2, 3])
        self.assertEqual(self.db.claim({"friend"}, now=200)[0]["username"], "group@chatroom")

    def test_cursor_and_messages_rollback_together(self):
        with self.assertRaises((TypeError, ValueError)):
            self.ingest("friend", [msg(1), dict(local_id=2, text=object())])
        self.assertIsNone(self.db.cursor("account", "friend", "message/message_0.db"))
        self.assertEqual(self.db.count(), 0)

    def test_failure_backoff_does_not_allow_later_messages_to_overtake(self):
        self.ingest("friend", [msg(1)])
        batch = self.db.claim(set(), now=200)
        self.db.complete(batch, "retry", now=200)
        self.ingest("friend", [msg(2)])
        self.assertIsNone(self.db.claim(set(), now=201))
        self.assertEqual(self.db.claim(set(), now=210)[0]["message"]["local_id"], 1)

    def test_processing_recovers_and_sent_events_do_not_replay(self):
        self.ingest("friend", [msg(1)])
        batch = self.db.claim(set(), now=200)
        self.db.close()
        self.db = Inbox(self.path)
        self.assertEqual(self.db.recover(set()), 1)
        batch = self.db.claim(set(), now=200)
        self.db.complete(batch, "unconfirmed")
        self.assertEqual(self.db.count(), 0)

    def test_accounts_and_shards_are_isolated(self):
        self.ingest("friend", [msg(1)])
        self.db.ingest("other", "friend", "message/message_1.db", "friend", [msg(1)], 4)
        self.assertEqual(self.db.count(), 2)
        batch = self.db.claim(set(), account="account", now=200)
        self.assertEqual({row["account"] for row in batch}, {"account"})
        self.assertEqual(self.db.count("other"), 1)

    def test_saved_batch_does_not_absorb_new_messages_on_retry(self):
        self.ingest("friend", [msg(1)])
        batch = self.db.claim(set(), now=200)
        self.db.save_reply(batch, "cached response")
        self.db.complete(batch, "retry", now=200)
        self.ingest("friend", [msg(2, stamp=101)])
        retried = self.db.claim(set(), now=210)
        self.assertEqual([r["message"]["local_id"] for r in retried], [1])
        self.assertEqual(retried[0]["reply"], "cached response")


if __name__ == "__main__":
    unittest.main()
