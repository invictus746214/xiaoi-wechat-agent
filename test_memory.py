import time
import unittest

import agent
from store import Store, today


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_self_facts_are_seeded(self):
        facts = "\n".join(self.store.self_facts())
        self.assertIn("小i", facts)
        self.assertIn("invictus", facts)
        self.assertIn("Linux 微信", facts)

    def test_search_memories_finds_related_and_skips_unrelated(self):
        self.store.add_message("s1", "user", "明天深圳天气怎么样")
        self.store.add_message("s1", "assistant", "深圳明天有雨，记得带伞")
        self.store.add_message("s1", "user", "我家猫今天又拆家了")
        self.store.add_message("s2", "user", "明天深圳天气怎么样")
        found = self.store.search_memories("s1", "天气如何", 6)
        text = "\n".join(item[1] for item in found)
        self.assertIn("深圳", text)
        self.assertNotIn("猫", text)
        self.assertNotIn("s2", text)

    def test_daily_digest_roundtrip(self):
        now = time.time()
        self.store.add_message("s1", "user", "今天去看了展", now=now)
        self.store.add_message("s1", "assistant", "看的什么展呀", now=now)
        digest = self.store.refresh_daily_digest("s1", today(now), now=now)
        self.assertIn("看了展", digest)
        self.assertEqual(digest, self.store.daily_summary("s1", today(now)))

    def test_build_messages_injects_self_and_memories(self):
        messages = agent.build_messages(
            "人格", ["对方喜欢猫"], [], "在吗", "2026-10-29 17:30",
            memories=[("user", "上次说想去看展")],
            self_facts=["我是小i，住在微信里"],
            daily="昨天聊了猫",
        )
        system = messages[0]["content"]
        self.assertIn("我是小i", system)
        self.assertIn("昨天聊了猫", system)
        self.assertIn("上次说想去看展", system)
        self.assertIn("对方喜欢猫", system)


if __name__ == "__main__":
    unittest.main()
