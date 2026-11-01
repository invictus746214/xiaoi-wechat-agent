import time
import unittest

import bridge
from store import Store


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.cfg = {"delivery": {
            "quota_enabled": True, "max_per_minute": 2, "max_per_10min": 10,
            "private_min_interval_seconds": 0, "group_min_interval_seconds": 0,
        }}
        self.job = {"session_key": "s1", "kind": "private"}

    def tearDown(self):
        self.store.close()

    def _confirmed(self, delivery_id, at):
        self.store.start_delivery(delivery_id, "s1", delivery_id, "hi", "hello", now=at)
        self.store.set_delivery_state(delivery_id, "confirmed", now=at)

    def test_minute_quota_defers(self):
        now = 1000.0
        self._confirmed("a", now - 40)
        self._confirmed("b", now - 20)
        self.assertGreater(bridge.quota_wait(self.store, self.job, self.cfg, now=now), 0)

    def test_under_quota_sends(self):
        now = 1000.0
        self._confirmed("a", now - 40)
        self.assertEqual(0, bridge.quota_wait(self.store, self.job, self.cfg, now=now))

    def test_min_interval_defers(self):
        now = 1000.0
        self._confirmed("a", now - 0.5)
        self.cfg["delivery"]["max_per_minute"] = 10
        self.cfg["delivery"]["private_min_interval_seconds"] = 2
        wait = bridge.quota_wait(self.store, self.job, self.cfg, now=now)
        self.assertTrue(1 <= wait <= 2)

    def test_disabled_quota_sends(self):
        self.cfg["delivery"]["quota_enabled"] = False
        self._confirmed("a", 999.0)
        self.assertEqual(0, bridge.quota_wait(self.store, self.job, self.cfg, now=1000.0))


class DeliveryIdTests(unittest.TestCase):
    def test_delivery_key_is_stable_and_distinct(self):
        first = bridge.delivery_key("session", "event", "reply")
        self.assertEqual(first, bridge.delivery_key("session", "event", "reply"))
        self.assertNotEqual(first, bridge.delivery_key("session", "event", "other"))

    def test_silent_marker(self):
        self.assertTrue(bridge.is_silent_reply("SILENT"))
        self.assertTrue(bridge.is_silent_reply(" [SILENT] "))
        self.assertTrue(bridge.is_silent_reply("SILENT。"))
        self.assertFalse(bridge.is_silent_reply("silent night"))
        self.assertFalse(bridge.is_silent_reply("我不想说话"))


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_state_machine_and_count(self):
        now = time.time()
        self.store.start_delivery("d1", "s1", "e1", "hi", "hello", now=now)
        self.assertEqual("queued", self.store.delivery("d1")["state"])
        self.store.set_delivery_state("d1", "sending", now=now)
        self.assertEqual(0, self.store.delivery_count("s1", now - 10))
        self.store.set_delivery_state("d1", "confirmed", now=now)
        self.assertEqual(1, self.store.delivery_count("s1", now - 10))
        self.assertEqual(0, self.store.delivery_count("s2", now - 10))

    def test_recovery_marks_inflight_unconfirmed_and_blocks_replay(self):
        now = time.time()
        self.store.start_delivery("d2", "s1", "e2", "hi", "hello", now=now)
        self.store.set_delivery_state("d2", "sending", now=now)
        self.assertEqual(1, self.store.recover_deliveries(now=now))
        self.assertEqual("unconfirmed", self.store.delivery("d2")["state"])
        self.assertTrue(self.store.seen_event("e2"))

    def test_queued_survives_recovery(self):
        self.store.start_delivery("d3", "s1", "e3", "hi", "hello")
        self.assertEqual(0, self.store.recover_deliveries())
        self.assertEqual("queued", self.store.delivery("d3")["state"])
        self.assertFalse(self.store.seen_event("e3"))

    def test_session_cursor(self):
        self.assertIsNone(self.store.cursor("s1"))
        self.store.set_cursor("s1", 42)
        self.assertEqual(42, self.store.cursor("s1"))


if __name__ == "__main__":
    unittest.main()
