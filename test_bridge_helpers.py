"""Helpers used by the reply path and the new config surface."""
import unittest
from unittest.mock import patch

import bridge
import settings
from store import Store


class MediaTests(unittest.TestCase):
    def test_image_stub(self):
        self.assertTrue(bridge.is_image_stub("[图片]"))
        self.assertTrue(bridge.is_image_stub("张三：图片"))
        self.assertTrue(bridge.is_image_stub("[动画表情]"))
        self.assertTrue(bridge.is_image_stub("[表情包]"))
        self.assertTrue(bridge.is_image_stub("Animated Stickers"))
        self.assertTrue(bridge.is_media_stub("Animated Stickers"))
        self.assertFalse(bridge.is_image_stub("[语音]"))
        self.assertFalse(bridge.is_image_stub("图片怎么发"))

    def test_bubble_matches_mention_prefix(self):
        self.assertTrue(bridge.bubble_matches("@张三 好的", "好的"))
        self.assertTrue(bridge.bubble_matches("好的", "好的"))
        self.assertFalse(bridge.bubble_matches("@张三 好的", "不行"))
        self.assertFalse(bridge.bubble_matches("", "好的"))

    def test_clean_image_text_rejects_noise(self):
        self.assertEqual("会议 3 点", bridge.clean_image_text(" 会议\n3 点 "))
        self.assertEqual("", bridge.clean_image_text("...."))
        self.assertEqual("", bridge.clean_image_text(""))

    def test_describe_image_prefers_vision(self):
        ui = FakeUI()
        text, source = bridge.describe_image(
            ui, FakeRect(), {"vision": {"enabled": True}}, lambda _p, _prompt: "一只猫", FakeOcr("不该用到")
        )
        self.assertEqual(("一只猫", "vision"), (text, source))
        self.assertEqual(1, ui.calls)

    def test_sticker_frames_are_deduplicated_and_caption_cached(self):
        ui, store = FakeUI(), Store(":memory:")
        cfg = {"vision": {"enabled": True, "model": "flash"}}
        calls = []
        try:
            with patch("bridge.vision.image_hash", return_value="image-one"):
                self.assertEqual(("猫在摆手", "vision"), bridge.describe_image(
                    ui, FakeRect(), cfg, lambda paths, prompt: calls.append((len(paths), prompt)) or "猫在摆手",
                    store=store, sticker=True))
                self.assertEqual(("猫在摆手", "vision_cache"), bridge.describe_image(
                    ui, FakeRect(), cfg, lambda *_: self.fail("cache miss"), store=store, sticker=True))
            self.assertEqual(1, calls[0][0])
            self.assertIn("情绪", calls[0][1])
        finally:
            store.close()

    def test_unclear_sticker_is_skipped_without_ocr_guess(self):
        self.assertEqual(("", "none"), bridge.describe_image(
            FakeUI(), FakeRect(), {"vision": {"enabled": True}},
            lambda *_: "SKIP", FakeOcr("不可靠文字"), sticker=True))

    def test_animated_sticker_does_not_reuse_first_frame_cache(self):
        class AnimatedUI(FakeUI):
            def capture_region(self, rect, path):
                self.calls += 1
                with open(path, "wb") as target:
                    target.write(str(self.calls).encode())
                return True
        ui, store = AnimatedUI(), Store(":memory:")
        try:
            with patch("bridge.vision.image_hash", return_value="same-first-frame"):
                for _ in range(2):
                    self.assertEqual(("动画猫", "vision"), bridge.describe_image(
                        ui, FakeRect(), {"vision": {"enabled": True, "model": "flash"}},
                        lambda paths, _: "动画猫" if len(paths) == 3 else "", store=store, sticker=True))
        finally:
            store.close()

    def test_visible_chat_context_precedes_image(self):
        class Rect:
            def __init__(self, y): self.y = y
        class UI:
            right_list = object()
            def rows(self, _): return [("先看看这个", Rect(100), None), ("好呀", Rect(150), None), ("[表情包]", Rect(200), None)]
            def incoming_state(self, text, _): return "outgoing" if text == "好呀" else "incoming"
        self.assertEqual("对方：先看看这个\n我：好呀", bridge.visible_context(UI(), Rect(200)))

    def test_mention_matches_only_the_bot_name(self):
        self.assertTrue(bridge.mentioned_self("张三：@小i 你看看", "小i"))
        self.assertFalse(bridge.mentioned_self("张三：@小明 你看看", "小i"))
        self.assertFalse(bridge.mentioned_self("张三：@小iphone 你看看", "小i"))

    def test_describe_image_falls_back_to_ocr(self):
        ui = FakeUI()
        text, source = bridge.describe_image(ui, FakeRect(), None, None, FakeOcr("会议 3 点"))
        self.assertEqual(("会议 3 点", "ocr"), (text, source))

    def test_describe_image_falls_back_when_vision_fails(self):
        ui = FakeUI()
        text, source = bridge.describe_image(
            ui, FakeRect(), {"vision": {"enabled": True}}, lambda _p, _prompt: (_ for _ in ()).throw(RuntimeError()),
            FakeOcr("截图文字"),
        )
        self.assertEqual(("截图文字", "ocr"), (text, source))

    def test_describe_image_without_any_reader(self):
        ui = FakeUI()
        self.assertEqual(("", "none"), bridge.describe_image(ui, FakeRect(), None, None, None))

    def test_describe_image_handles_capture_failure(self):
        text, source = bridge.describe_image(FakeUI(fail=True), FakeRect(), None, None, FakeOcr("x"))
        self.assertEqual(("", "none"), (text, source))


class FakeRect:
    x, y, width, height = 0, 0, 10, 10


def boxes(x, width, pane_width=800):
    rect = FakeRect()
    rect.x, rect.y, rect.width, rect.height = x, 0, width, 100
    pane = FakeRect()
    pane.x, pane.y, pane.width, pane.height = 0, 0, pane_width, 600
    return rect, pane


class ImageDirectionTests(unittest.TestCase):
    def test_left_aligned_is_incoming(self):
        self.assertEqual("incoming", bridge.classify_by_position(*boxes(20, 200)))

    def test_right_aligned_is_outgoing(self):
        self.assertEqual("outgoing", bridge.classify_by_position(*boxes(580, 200)))

    def test_centred_is_unknown(self):
        self.assertEqual("unknown", bridge.classify_by_position(*boxes(350, 100)))

    def test_full_width_is_unknown(self):
        self.assertEqual("unknown", bridge.classify_by_position(*boxes(0, 800)))

    def test_missing_pane_is_unknown(self):
        self.assertEqual("unknown", bridge.classify_by_position(boxes(20, 200)[0], None))


def pixels_with_block(width, height, start, end, value=(220, 30, 30)):
    data = bytearray([240] * (width * height * 3))
    for y in range(height):
        for x in range(start, end):
            offset = (y * width + x) * 3
            data[offset:offset + 3] = bytes(value)
    return data


class ContentBoundsTests(unittest.TestCase):
    def setUp(self):
        self.rect = FakeRect()
        self.rect.x, self.rect.y, self.rect.width, self.rect.height = 0, 0, 200, 40

    def test_left_block(self):
        data = pixels_with_block(200, 40, 5, 45)
        bounds = bridge.content_bounds(data, 200 * 3, 3, self.rect)
        self.assertIsNotNone(bounds)
        self.assertLess(bounds[0], 20)
        self.assertLess(bounds[1], 60)
        self.assertEqual("incoming", bridge.direction_from_bounds(bounds, self.rect))

    def test_right_block(self):
        data = pixels_with_block(200, 40, 150, 195)
        bounds = bridge.content_bounds(data, 200 * 3, 3, self.rect)
        self.assertEqual("outgoing", bridge.direction_from_bounds(bounds, self.rect))

    def test_full_width_is_unknown(self):
        data = pixels_with_block(200, 40, 0, 200)
        bounds = bridge.content_bounds(data, 200 * 3, 3, self.rect)
        self.assertEqual("unknown", bridge.direction_from_bounds(bounds, self.rect))

    def test_blank_row_has_no_bounds(self):
        self.assertIsNone(bridge.content_bounds(bytearray([240] * (200 * 40 * 3)), 200 * 3, 3, self.rect))


class FakeUI:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def capture_region(self, _rect, path):
        self.calls += 1
        if self.fail:
            raise RuntimeError("capture_failed")
        with open(path, "wb") as target:
            target.write(b"png")
        return True


class FakeOcr:
    def __init__(self, text="", available=True):
        self.text = text
        self.available_flag = available

    def available(self):
        return self.available_flag

    def read(self, _path):
        return self.text


class FrameChoiceTests(unittest.TestCase):
    def _option(self, name, width, height, score, active):
        rect = FakeRect()
        rect.x, rect.y, rect.width, rect.height = 0, 0, width, height
        return {"name": name, "rect": rect, "score": score, "active": active}

    def test_prefers_main_window_over_moments(self):
        main = self._option("Weixin", 878, 640, 2, False)
        moments = self._option("Moments", 440, 560, 2, True)
        self.assertIs(main, bridge.choose_chat_group([moments, main]))

    def test_largest_weixin_wins(self):
        small = self._option("Weixin", 400, 300, 2, True)
        large = self._option("Weixin", 878, 640, 2, False)
        self.assertIs(large, bridge.choose_chat_group([small, large]))


class BatchMessageTests(unittest.TestCase):
    def test_single_message_unchanged(self):
        self.assertEqual("在吗", bridge.build_batch_message(["在吗"]))

    def test_multiple_messages_are_merged(self):
        text = bridge.build_batch_message(["在吗", "晚上吃啥", "火锅？"])
        self.assertIn("连续发了 3 条", text)
        self.assertIn("- 火锅？", text)
        self.assertEqual(3, len([line for line in text.splitlines() if line.startswith("- ")]))

    def test_empty_batch(self):
        self.assertEqual("", bridge.build_batch_message([]))


class RowRect:
    def __init__(self, y):
        self.x, self.y, self.width, self.height = 0, y, 400, 20


class BatchUI:
    def __init__(self, names, directions=None):
        self.right_list = None
        self._rows = [(name, RowRect(index * 30), None) for index, name in enumerate(names)]
        self._directions = directions or {}

    def rows(self, _container):
        return list(self._rows)

    def incoming_state(self, text, _rect):
        return self._directions.get(text, "incoming")


class CollectBatchTests(unittest.TestCase):
    def test_collects_back_to_the_boundary(self):
        ui = BatchUI(["A", "B", "C"])
        batch, newest = bridge.collect_batch(ui, "C", RowRect(60), bridge.digest("A"))
        self.assertEqual(["B", "C"], batch)
        self.assertEqual(bridge.digest("C"), newest)

    def test_stops_at_outgoing(self):
        ui = BatchUI(["A", "B", "C"], {"B": "outgoing"})
        batch, _ = bridge.collect_batch(ui, "C", RowRect(60), bridge.digest("A"))
        self.assertEqual(["C"], batch)

    def test_without_boundary_only_takes_trigger(self):
        ui = BatchUI(["A", "B", "C"])
        batch, _ = bridge.collect_batch(ui, "C", RowRect(60), None)
        self.assertEqual(["C"], batch)

    def test_unread_burst_without_boundary(self):
        ui = BatchUI(["A", "B", "C"])
        batch, _ = bridge.collect_batch(ui, "C", RowRect(60), None, unread=3)
        self.assertEqual(["A", "B", "C"], batch)

    def test_stops_at_media(self):
        ui = BatchUI(["A", "图片", "C"])
        batch, _ = bridge.collect_batch(ui, "C", RowRect(60), bridge.digest("A"))
        self.assertEqual(["C"], batch)


class PreviewTests(unittest.TestCase):
    def test_pinned_unread_badge_and_preview(self):
        row = "invictus\nPinned to top\n[5]\n先去看那个展\n22:33"
        self.assertEqual(5, bridge.unread_count(row))
        self.assertTrue(bridge.preview_contains(row, "先去看那个展"))

    def test_badge_without_preview_is_not_counted(self):
        self.assertEqual(0, bridge.unread_count("Alice\n[1]\n18:05"))
        self.assertTrue(bridge.preview_contains("Alice\n[1]\n18:05", "[1]"))


class InterjectTests(unittest.TestCase):
    def test_probability_and_cooldown(self):
        cfg = {"policy": {"group_interject_enabled": True, "group_interject_probability": 1.0,
                          "group_interject_min_seconds": 600}}
        job = {"identity": "g", "event_id": "0000abc"}
        self.assertTrue(bridge.should_interject(job, cfg, {}, 1000))
        self.assertFalse(bridge.should_interject(job, cfg, {"g": 900}, 1000))
        cfg["policy"]["group_interject_probability"] = 0.0
        self.assertFalse(bridge.should_interject(job, cfg, {}, 1000))


class EngagementTests(unittest.TestCase):
    def test_related_topic_overlap(self):
        topic = bridge.message_terms("@小i 5090显卡现在多少钱")
        self.assertTrue(bridge.related_to_topic(topic, "那显卡价格还会降吗"))
        self.assertFalse(bridge.related_to_topic(topic, "今天中午吃什么"))

    def test_empty_topic_does_not_block(self):
        self.assertTrue(bridge.related_to_topic(set(), "随便说点什么"))


class MentionTests(unittest.TestCase):
    def test_mention_with_text_after_name(self):
        self.assertTrue(bridge.mentioned_self("@小i 你好", "小i"))
        self.assertTrue(bridge.mentioned_self("@小i在吗", "小i"))
        self.assertTrue(bridge.mentioned_self("@小i\u2005你好", "小i"))

    def test_mention_aliases_and_badge(self):
        self.assertTrue(bridge.mentioned_self("@小杜 你好", ["小i", "小杜"]))
        self.assertTrue(bridge.mentioned_self("[有人@我]", "小i"))

    def test_other_people_are_not_me(self):
        self.assertFalse(bridge.mentioned_self("@别人 你好", "小i"))
        self.assertTrue(bridge.mentioned_self("小i在吗", "小i"))
        self.assertFalse(bridge.mentioned_self("别人说小i在吗", "小i"))

    def test_leading_name_without_at(self):
        self.assertTrue(bridge.mentioned_self("小i 你好", "小i"))
        self.assertTrue(bridge.mentioned_self("invictus: 小i 在吗", "小i"))


class RelayCommandTests(unittest.TestCase):
    def test_clean_db_text(self):
        self.assertEqual("你好", bridge.clean_db_text("wxid_abc:\n你好"))
        self.assertEqual("你好", bridge.clean_db_text("123@chatroom:\n你好"))
        self.assertEqual("你好", bridge.clean_db_text("你好"))
    def test_parse_relay(self):
        self.assertEqual(("小杜", "晚上一起吃饭"), bridge.parse_relay_command("去跟小杜说晚上一起吃饭"))
        self.assertEqual(("小杜", "你好"), bridge.parse_relay_command("帮我给小杜发：你好"))
        self.assertEqual(("小杜", "今晚不去了"), bridge.parse_relay_command("告诉小杜：今晚不去了"))

    def test_not_a_relay_command(self):
        self.assertIsNone(bridge.parse_relay_command("给我朋友圈点个赞"))
        self.assertIsNone(bridge.parse_relay_command("今天吃什么"))
        self.assertIsNone(bridge.parse_relay_command("给我说个笑话"))
        self.assertIsNone(bridge.parse_relay_command("给你说个事"))

    def test_relay_allowlist(self):
        cfg = {"relay": {"allowed_titles": ["invictus"]}}
        self.assertTrue(bridge.relay_allowed("invictus", cfg))
        self.assertFalse(bridge.relay_allowed("陌生人", cfg))
        self.assertFalse(bridge.relay_allowed("invictus", {"relay": {"allowed_titles": []}}))

    def test_replybox_command(self):
        self.assertEqual("谢谢欢迎", bridge.parse_replybox_command("回复她：谢谢欢迎"))
        self.assertEqual("好的", bridge.parse_replybox_command("回复这条: 好的"))
        self.assertEqual("", bridge.parse_replybox_command("回复我一下"))

    def test_read_command(self):
        self.assertEqual("小杜", bridge.parse_read_command("看看小杜最近说了什么"))
        self.assertEqual("小杜", bridge.parse_read_command("小杜最近说了什么"))
        self.assertIsNone(bridge.parse_read_command("给我说个笑话"))

    def test_inbox_command(self):
        self.assertTrue(bridge.parse_inbox_command("谁找我了"))
        self.assertTrue(bridge.parse_inbox_command("有未读吗"))
        self.assertFalse(bridge.parse_inbox_command("今天吃什么"))


class MomentCommandTests(unittest.TestCase):
    def test_author_matches_aliases(self):
        self.assertTrue(bridge.moment_author_matches("小杜", "invictus", ["小杜"]))
        self.assertTrue(bridge.moment_author_matches("invictus", "invictus", []))
        self.assertFalse(bridge.moment_author_matches("SAD4NESS", "invictus", ["小杜"]))

    def test_like_command(self):
        self.assertEqual(("like", ""), bridge.parse_moment_command("能不能去我朋友圈点个赞"))
        self.assertEqual(("like", ""), bridge.parse_moment_command("给我朋友圈点一个赞"))
        self.assertEqual(("like", ""), bridge.parse_moment_command("帮我朋友圈赞一下"))

    def test_comment_command(self):
        self.assertEqual(("comment", "这双鞋挺好看"),
                         bridge.parse_moment_command("去我朋友圈评论：这双鞋挺好看"))
        self.assertEqual(("comment", "好"), bridge.parse_moment_command("评论我朋友圈：好"))

    def test_not_a_command(self):
        self.assertIsNone(bridge.parse_moment_command("朋友圈是什么"))
        self.assertIsNone(bridge.parse_moment_command("帮我评论一下"))
        self.assertIsNone(bridge.parse_moment_command("今天天气不错"))

    def test_reply_mapping(self):
        self.assertIn("点了个赞", bridge.moment_reply("like", "liked"))
        self.assertIn("已经赞过", bridge.moment_reply("like", "already"))
        self.assertIn("评论好了", bridge.moment_reply("comment", "commented"))


class ToolTests(unittest.TestCase):
    def test_weather_schema_is_exposed(self):
        import agent

        names = [item["function"]["name"] for item in agent.Tools().schemas()]
        self.assertIn("get_weather", names)
        self.assertIn("get_time", names)

    def test_weather_without_city_is_safe(self):
        import agent

        self.assertIn("城市", agent.Tools().call("get_weather", {}))

    def test_search_and_fetch_schemas(self):
        import agent

        names = [item["function"]["name"] for item in agent.Tools().schemas()]
        self.assertIn("web_search", names)
        self.assertIn("web_fetch", names)

    def test_private_url_is_rejected(self):
        import agent

        self.assertFalse(agent._public_url("http://127.0.0.1/"))
        self.assertFalse(agent._public_url("file:///etc/passwd"))
        self.assertIn("不能打开", agent.Tools().call("web_fetch", {"url": "http://127.0.0.1/"}))


class ConfigTests(unittest.TestCase):
    def test_moments_defaults_are_off_and_dry(self):
        cfg = settings.normalize({})
        self.assertFalse(cfg["moments"]["enabled"])
        self.assertTrue(cfg["moments"]["dry_run"])
        self.assertFalse(cfg["moments"]["like_enabled"])
        self.assertFalse(cfg["moments"]["comment_enabled"])
        self.assertFalse(cfg["vision"]["enabled"])

    def test_moments_values_are_clamped(self):
        cfg = settings.normalize({
            "moments": {
                "scan_minutes": 0,
                "scan_pages": 99,
                "max_posts": 999,
                "like_daily_cap": 100000,
                "min_interval_seconds": -5,
                "authors": [" 张三 ", 7],
            }
        })
        self.assertEqual(1, cfg["moments"]["scan_minutes"])
        self.assertEqual(10, cfg["moments"]["scan_pages"])
        self.assertEqual(50, cfg["moments"]["max_posts"])
        self.assertEqual(500, cfg["moments"]["like_daily_cap"])
        self.assertEqual(0, cfg["moments"]["min_interval_seconds"])
        self.assertEqual(["张三"], cfg["moments"]["authors"])

    def test_group_mention_reply_defaults_on(self):
        self.assertTrue(settings.normalize({})["policy"]["group_mention_reply"])


if __name__ == "__main__":
    unittest.main()
