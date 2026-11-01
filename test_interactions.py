import time
import unittest
from types import SimpleNamespace

import bridge
from store import Store


class InteractionParseTests(unittest.TestCase):
    def setUp(self):
        self.posts = [
            SimpleNamespace(text="测试帖2136，马上删"),
            SimpleNamespace(text="DeepSeek 微信形象上线第一天，蓝毛小i来啦～"),
        ]

    def test_comment_reply(self):
        item = bridge.parse_interaction_name(
            "invictus 13 minute(s) ago Reply 亖k：不觉得可爱吗所以没删 测试帖2136，马上删",
            self.posts,
        )
        self.assertIsNotNone(item)
        self.assertEqual("invictus", item["author"])
        self.assertEqual("comment", item["action"])
        self.assertEqual("测试帖2136，马上删", item["post_text"])
        self.assertIn("不觉得可爱", item["body"])

    def test_like(self):
        item = bridge.parse_interaction_name(
            "SAD4NESS 2 hour(s) ago 赞了你的朋友圈 测试帖2136，马上删",
            self.posts,
        )
        self.assertIsNotNone(item)
        self.assertEqual("like", item["action"])

    def test_unknown_format(self):
        self.assertIsNone(bridge.parse_interaction_name("随便一句话", self.posts))


class InteractionStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_dedupe(self):
        self.assertFalse(self.store.interaction_seen("h1"))
        self.store.mark_interaction("h1", "invictus", "你好", "帖子", "comment", now=time.time())
        self.assertTrue(self.store.interaction_seen("h1"))
        self.assertEqual(1, self.store.interaction_count())
        self.store.mark_interaction("h1", "invictus", "你好", "帖子", "comment")
        self.assertEqual(1, self.store.interaction_count())


if __name__ == "__main__":
    unittest.main()
