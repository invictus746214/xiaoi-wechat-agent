import time
import unittest

import bridge
import policy
import settings
from store import Store


class GroupInteractionTests(unittest.TestCase):
    def test_mention_followup_and_global_interjection_limit(self):
        cfg = settings.normalize({"policy": {"group_mention_only": True, "group_self_name": "小i"},
                                  "proactive": {"enabled": True, "group_interjections": True}})
        store = Store(":memory:")
        now = time.time()
        job = {"kind": "group", "is_mention": False, "is_media": False,
               "session_key": "one-group", "event_id": "0000000000000000"}
        try:
            self.assertEqual("proactive", bridge.group_reply_mode(job, cfg, store, now))
            self.assertEqual(1, store.count_events("group_interject:", now - 1))
            self.assertEqual("proactive", bridge.group_reply_mode(job, cfg, store, now))
            job["event_id"] = "0000000000000001"
            self.assertEqual("proactive", bridge.group_reply_mode(job, cfg, store, now))
            self.assertEqual(2, store.count_events("group_interject:", now - 1))
            self.assertEqual((True, "ok"), policy.allowed(cfg["policy"],
                {"kind": "group", "group_reply_mode": "proactive"}))
            store.add_message("one-group", "assistant", "刚回复", now=now - 100)
            self.assertEqual("followup", bridge.group_reply_mode(job, cfg, store, now))
            job["is_mention"] = True
            self.assertEqual("mention", bridge.group_reply_mode(job, cfg, store, now))
            job["is_mention"] = False
            store.mark_event("group_interject:one", now=now)
            store.mark_event("group_interject:two", now=now)
            job["session_key"] = "other-group"
            self.assertEqual("skip", bridge.group_reply_mode(job, cfg, store, now))
            self.assertEqual((False, "group_not_mentioned"), policy.allowed(cfg["policy"],
                {"kind": "group", "group_reply_mode": "skip"}))
        finally:
            store.close()

    def test_pass_control_token_is_never_sent(self):
        self.assertTrue(bridge.proactive_pass("PASS。"))
        self.assertTrue(bridge.proactive_pass("PASS\n暂时不接话"))
        self.assertFalse(bridge.proactive_pass("可以试试这个办法"))
