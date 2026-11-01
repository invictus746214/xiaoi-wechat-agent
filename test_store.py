import unittest

from store import Store, session_key


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:", retention_days=2)

    def tearDown(self):
        self.store.close()

    def test_event_dedupe_is_persistent_inside_the_same_store(self):
        self.assertFalse(self.store.seen_event("evt-1"))
        self.store.mark_event("evt-1", now=1000.0)
        self.assertTrue(self.store.seen_event("evt-1"))

    def test_recent_returns_oldest_first_and_respects_limit(self):
        for index in range(5):
            self.store.add_message("k", "user" if index % 2 == 0 else "assistant", "msg%d" % index, now=1000 + index)
        self.assertEqual(self.store.recent("k", 3), [("user", "msg2"), ("assistant", "msg3"), ("user", "msg4")])
        self.assertEqual(self.store.recent("k", 0), [])

    def test_facts_are_deduplicated_per_conversation(self):
        self.assertTrue(self.store.add_fact("k", "喜欢咖啡", now=1.0))
        self.assertFalse(self.store.add_fact("k", "喜欢咖啡", now=2.0))
        self.assertTrue(self.store.add_fact("other", "喜欢咖啡", now=3.0))
        self.assertEqual(self.store.facts("k"), ["喜欢咖啡"])

    def test_usage_accumulates_on_the_same_day(self):
        self.store.add_usage(10, 5, now=1000.0)
        self.store.add_usage(1, 2, now=1001.0)
        usage = self.store.usage_today(now=1002.0)
        self.assertEqual(usage, {"calls": 2, "tokens_in": 11, "tokens_out": 7})

    def test_purge_drops_rows_past_retention(self):
        now = 10 * 86400.0
        self.store.add_message("k", "user", "old", now=now - 5 * 86400)
        self.store.add_message("k", "user", "new", now=now - 3600)
        self.store.add_fact("k", "旧的", now=now - 5 * 86400)
        self.assertEqual(self.store.purge(now=now), 2)
        self.assertEqual(self.store.recent("k"), [("user", "new")])
        self.assertEqual(self.store.facts("k"), [])

    def test_summary_round_trip_and_high_water_mark(self):
        self.assertEqual(self.store.get_summary("k"), ("", 0))
        self.store.add_message("k", "user", "a", now=1.0)
        self.store.add_message("k", "assistant", "b", now=2.0)
        self.assertEqual(self.store.max_message_id("k"), 2)
        self.store.set_summary("k", "聊了 a", 2)
        self.assertEqual(self.store.get_summary("k"), ("聊了 a", 2))
        self.assertEqual(self.store.messages_after("k", 2), [])
        self.store.add_message("k", "user", "c", now=3.0)
        self.assertEqual([row[2] for row in self.store.messages_after("k", 2)], ["c"])

    def test_pending_round_trip(self):
        self.store.save_pending("k1", "rh", "id", title="Alice", created_at=100.0, attempts=2, due_at=50.0)
        rows = self.store.load_pending()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "k1")
        self.assertEqual(rows[0]["attempts"], 2)
        self.store.delete_pending("k1")
        self.assertEqual(self.store.pending_count(), 0)

    def test_pending_replay_respects_the_window(self):
        now = 1000.0
        self.store.save_pending("old", "rh", "id", created_at=now - 3600)
        self.store.save_pending("new", "rh", "id", created_at=now - 10)
        self.assertEqual([row["key"] for row in self.store.load_pending(since=now - 60)], ["new"])

    def test_purge_also_drops_stale_pending(self):
        now = 10 * 86400.0
        self.store.save_pending("old", "rh", "id", created_at=now - 5 * 86400)
        self.assertEqual(self.store.purge(now=now), 1)
        self.assertEqual(self.store.pending_count(), 0)

    def test_session_keys_are_stable_and_not_reversible(self):
        key = session_key("/row-a\nAlice")
        self.assertEqual(key, session_key("/row-a\nAlice"))
        self.assertNotEqual(key, session_key("/row-a\nBob"))
        self.assertNotIn("Alice", key)
        self.assertEqual(len(key), 32)
