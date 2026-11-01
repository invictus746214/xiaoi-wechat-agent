import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

import bridge
from bridge import (
    MAX_PENDING_ATTEMPTS,
    WeChatUI,
    is_non_message_row,
    is_system_notification,
    preview_contains,
    preview_matches,
    stall_status,
    update_pending,
    choose_pending,
    refresh_latest_chats,
)


class PreviewMatchingTests(unittest.TestCase):
    def test_truncated_private_preview_still_confirms(self):
        self.assertTrue(preview_contains("Alice\n这是一条很长的消息…\n18:05", "这是一条很长的消息，后面还有内容"))
        self.assertTrue(preview_contains("Alice\n这是一条很长的消息...\n18:05", "这是一条很长的消息，后面还有内容"))

    def test_truncated_group_preview_still_confirms(self):
        self.assertTrue(preview_contains("Group\n张三：这是一条很长的…\n18:05", "这是一条很长的消息，后面还有"))

    def test_prefix_without_ellipsis_is_not_confirmed(self):
        self.assertFalse(preview_contains("Alice\n你好\n18:05", "你好世界"))

    def test_exact_and_sender_forms_are_unchanged(self):
        self.assertTrue(preview_matches("hello", "hello"))
        self.assertTrue(preview_matches("张三：hello", "hello"))
        self.assertFalse(preview_matches("hello", "hello world"))


class NonMessageRowTests(unittest.TestCase):
    def test_wechat_system_notifications_are_not_replied_to(self):
        self.assertTrue(is_system_notification('"gavv" 拍了拍我'))
        self.assertTrue(is_system_notification('你已添加了朋友，以上是打招呼的消息。'))
        self.assertFalse(is_system_notification('你看，有人拍了拍我'))

    def test_timestamps_and_typing_rows_are_not_messages(self):
        self.assertTrue(is_non_message_row("18:05"))
        self.assertTrue(is_non_message_row("9:05"))
        self.assertTrue(is_non_message_row("对方正在输入…"))
        self.assertTrue(is_non_message_row("以下为新消息"))

    def test_ordinary_text_is_a_message(self):
        self.assertFalse(is_non_message_row("hello"))
        self.assertFalse(is_non_message_row("18:05 出发"))
        self.assertFalse(is_non_message_row(""))


class RowReadTests(unittest.TestCase):
    @staticmethod
    def _row(name):
        return SimpleNamespace(
            name=name,
            queryComponent=lambda: SimpleNamespace(
                getExtents=lambda _: SimpleNamespace(x=0, y=0, width=10, height=10)
            ),
        )

    def test_a_single_broken_row_does_not_stop_the_poll(self):
        class Container:
            childCount = 2
            calls = 0
            def clear_cache(self):
                pass
            def __getitem__(self, index):
                if index == 0:
                    raise RuntimeError("virtualized_row")
                return RowReadTests._row("ok")

        ui = WeChatUI.__new__(WeChatUI)
        ui.pyatspi = SimpleNamespace(DESKTOP_COORDS=0)
        rows = ui.rows(Container())
        self.assertEqual([row[0] for row in rows], ["ok"])

    def test_all_broken_rows_still_raise(self):
        class Container:
            childCount = 1
            def clear_cache(self):
                pass
            def __getitem__(self, index):
                raise RuntimeError("all_broken")

        ui = WeChatUI.__new__(WeChatUI)
        ui.pyatspi = SimpleNamespace(DESKTOP_COORDS=0)
        with self.assertRaises(RuntimeError):
            ui.rows(Container())

    def test_offscreen_chat_is_scrolled_into_view_before_click(self):
        name = "Alice\nhello"
        rect = SimpleNamespace(x=261, y=96, width=240, height=65)
        row = SimpleNamespace(path="/alice", getState=lambda: SimpleNamespace(contains=lambda _: False))
        taps, scrolls = [], []
        pointer = SimpleNamespace(
            move=lambda *_: None,
            scroll=lambda steps, up: (scrolls.append((steps, up)), setattr(rect, "y", 161)),
            tap=lambda x, y: taps.append((x, y)),
        )
        ui = WeChatUI.__new__(WeChatUI)
        ui.left_list = object()
        ui.pyatspi = SimpleNamespace(STATE_SELECTED=1)
        ui.window_active = lambda: True
        ui.rows = lambda _: [(name, rect, row)]
        ui.pointer = lambda: pointer
        bounds = SimpleNamespace(x=261, y=161, width=240, height=558)
        with patch("bridge.component_rect", return_value=bounds), patch("bridge.time.sleep"):
            self.assertEqual(("/alice", "Alice"), ui.click_row(bridge.digest(name)))
        self.assertEqual([(5, True)], scrolls)
        self.assertEqual([(381, 193)], taps)

    def test_offscreen_chat_never_clicks_search_bar_if_scroll_fails(self):
        name = "Alice\nhello"
        rect = SimpleNamespace(x=261, y=96, width=240, height=65)
        row = SimpleNamespace(path="/alice", getState=lambda: SimpleNamespace(contains=lambda _: False))
        taps = []
        pointer = SimpleNamespace(move=lambda *_: None, scroll=lambda *_: None,
                                  tap=lambda *point: taps.append(point))
        ui = WeChatUI.__new__(WeChatUI)
        ui.left_list = object()
        ui.pyatspi = SimpleNamespace(STATE_SELECTED=1)
        ui.window_active = lambda: True
        ui.rows = lambda _: [(name, rect, row)]
        ui.pointer = lambda: pointer
        with patch("bridge.component_rect", return_value=SimpleNamespace(x=261, y=161, width=240, height=558)), patch("bridge.time.sleep"):
            self.assertIsNone(ui.click_row(bridge.digest(name)))
        self.assertEqual([], taps)


class PendingRetryTests(unittest.TestCase):
    def test_chat_list_returns_to_newest_page_on_interval(self):
        moves, scrolls = [], []
        pointer = SimpleNamespace(move=lambda *xy: moves.append(xy),
                                  scroll=lambda steps, up: scrolls.append((steps, up)))
        ui = SimpleNamespace(left_list=object(), pyatspi=object(), window_active=lambda: True,
                             pointer=lambda: pointer)
        bounds = SimpleNamespace(x=260, y=160, width=240, height=560)
        with patch("bridge.component_rect", return_value=bounds), patch("bridge.time.monotonic", return_value=10.5):
            self.assertEqual(6.0, refresh_latest_chats(ui, 6.0, 10.0))
            self.assertEqual(10.5, refresh_latest_chats(ui, 5.0, 10.0))
        self.assertEqual([(380, 440)], moves)
        self.assertEqual([(12, True)], scrolls)

    def test_chat_list_scroll_failure_does_not_block_message_polling(self):
        pointer = SimpleNamespace(move=lambda *_: None,
                                  scroll=lambda *_, **__: (_ for _ in ()).throw(RuntimeError("x11")))
        ui = SimpleNamespace(left_list=object(), pyatspi=object(), window_active=lambda: True,
                             pointer=lambda: pointer)
        bounds = SimpleNamespace(x=260, y=160, width=240, height=560)
        with patch("bridge.component_rect", return_value=bounds):
            self.assertEqual(10.0, refresh_latest_chats(ui, 0.0, 10.0))

    def test_new_unread_chat_precedes_old_backlog(self):
        row = SimpleNamespace(path="/new")
        name = "New\n[1]\nhello\n11:01"
        identity = bridge.row_identity(row.path, "New")
        fresh = (bridge.digest(name), 0, 0.0, identity)
        pending = {"old": ("old-hash", 0, 0.0, "old-identity"), "new": fresh}
        self.assertEqual((fresh[0], identity), choose_pending(pending, [(name, None, row)], 100.0))

    def test_private_then_mention_then_group_chatter(self):
        names = ["Group\n[1]\nBob: hi\n11:03", "Group2\n[1]\nBob: @小i 看看\n11:02",
                 "Alice\n[1]\n你好\n11:01"]
        rows = [(name, None, SimpleNamespace(path="/" + str(i))) for i, name in enumerate(names)]
        pending = {}
        for name, _, row in rows:
            identity = bridge.row_identity(row.path, name.splitlines()[0])
            pending[identity] = (bridge.digest(name), 0, 0.0, identity)
        self.assertEqual((bridge.digest(names[2]), list(pending)[2]), choose_pending(pending, rows, 100, "小i"))
        pending.pop(list(pending)[2])
        self.assertEqual((bridge.digest(names[1]), list(pending)[1]), choose_pending(pending, rows, 100, "小i"))

    def test_due_time_is_respected_and_newest_old_work_is_next(self):
        pending = {"old": ("old-hash", 0, 0.0, "old-identity"),
                   "new": ("new-hash", 0, 101.0, "new-identity")}
        self.assertEqual(("old-hash", "old-identity"), choose_pending(pending, [], 100.0))
        self.assertEqual(("new-hash", "new-identity"), choose_pending(pending, [], 102.0))

    def test_candidate_is_dropped_after_the_attempt_cap(self):
        pending = {"row:x": ("abcdef0123456789", 0, 0.0, "identity")}
        seen, seen_set = deque(), set()
        for _ in range(MAX_PENDING_ATTEMPTS):
            if "row:x" in pending:
                update_pending(pending, "row:x", "retry", seen, seen_set, 100.0)
        self.assertNotIn("row:x", pending)
        self.assertIn("row:x", seen_set)

    def test_candidate_below_the_cap_stays_pending(self):
        pending = {"row:x": ("abcdef0123456789", 0, 0.0, "identity")}
        seen, seen_set = deque(), set()
        update_pending(pending, "row:x", "retry", seen, seen_set, 100.0)
        self.assertIn("row:x", pending)
        self.assertNotIn("row:x", seen_set)


    def test_draft_marker_no_longer_blocks_confirmation(self):
        self.assertTrue(preview_matches("[草稿]", "hello"))
        self.assertTrue(preview_matches("[草稿]hello", "hello"))
        self.assertFalse(preview_matches("[草稿]other", "hello"))


class DraftTests(unittest.TestCase):
    @staticmethod
    def _ui(value):
        class Composer:
            def __init__(self, text):
                self.value = text

            @property
            def characterCount(self):
                return len(self.value)

            def queryEditableText(self):
                return self

            def queryText(self):
                return self

            def getText(self, start, end):
                return self.value[start:end]

            def deleteText(self, start, end):
                self.value = self.value[:start] + self.value[end:]

        ui = WeChatUI.__new__(WeChatUI)
        ui.composer = Composer(value)
        return ui

    def test_leftover_reply_draft_is_cleared(self):
        ui = self._ui("收到")
        self.assertTrue(ui.clear_draft("收到"))
        self.assertEqual(ui.composer.value, "")

    def test_user_draft_is_never_deleted(self):
        ui = self._ui("我自己的草稿")
        self.assertFalse(ui.clear_draft("收到"))
        self.assertEqual(ui.composer.value, "我自己的草稿")


class ComposerWatchTests(unittest.TestCase):
    def test_small_composers_are_ignored(self):
        watch = bridge.ComposerWatch(threshold=500, seconds=120)
        self.assertIsNone(watch.observe(0, 0.0))
        self.assertIsNone(watch.observe(40, 10.0))
        self.assertIsNone(watch.observe(499, 200.0))

    def test_a_full_composer_turns_stuck_after_the_window(self):
        watch = bridge.ComposerWatch(threshold=500, seconds=120)
        self.assertIsNone(watch.observe(600, 0.0))
        self.assertIsNone(watch.observe(900, 60.0))
        self.assertEqual(watch.observe(2400, 130.0), "stuck")
        self.assertTrue(watch.stuck)
        self.assertIsNone(watch.observe(3000, 140.0))

    def test_clearing_the_composer_recovers(self):
        watch = bridge.ComposerWatch(threshold=500, seconds=120)
        watch.observe(600, 0.0)
        self.assertEqual(watch.observe(800, 200.0), "stuck")
        self.assertEqual(watch.observe(0, 210.0), "recovered")
        self.assertFalse(watch.stuck)
        self.assertIsNone(watch.observe(0, 220.0))

    def test_negative_reading_does_not_pause(self):
        watch = bridge.ComposerWatch(threshold=500, seconds=120)
        self.assertIsNone(watch.observe(-1, 0.0))
        self.assertIsNone(watch.observe(-1, 999.0))
        self.assertFalse(watch.stuck)


class ChatFrameChoiceTests(unittest.TestCase):
    def test_stray_window_without_a_composer_never_wins(self):
        chat = {"score": 2, "active": False, "group": "chat", "left": "L"}
        moments = {"score": 0, "active": True, "group": "moments", "left": "M"}
        self.assertEqual(bridge.choose_chat_group([moments, chat])["group"], "chat")

    def test_tie_falls_back_to_the_active_window(self):
        idle = {"score": 2, "active": False, "group": "idle", "left": "L"}
        active = {"score": 2, "active": True, "group": "active", "left": "L"}
        self.assertEqual(bridge.choose_chat_group([idle, active])["group"], "active")

    def test_tie_without_a_single_active_window_is_ambiguous(self):
        first = {"score": 2, "active": False, "group": "a", "left": "L"}
        second = {"score": 2, "active": False, "group": "b", "left": "L"}
        with self.assertRaises(RuntimeError):
            bridge.choose_chat_group([first, second])

    def test_no_candidates_raise(self):
        with self.assertRaises(RuntimeError):
            bridge.choose_chat_group([])


class StallStatusTests(unittest.TestCase):
    def test_active_window_is_quiet(self):
        self.assertEqual(stall_status(None, 0.0, True), (None, None))

    def test_first_inactive_poll_opens_the_window(self):
        self.assertEqual(stall_status(None, 10.0, False), (10.0, None))

    def test_stall_is_reported_once_per_threshold(self):
        self.assertEqual(stall_status(10.0, 15.0, False), (10.0, None))
        self.assertEqual(stall_status(10.0, 70.0, False), (70.0, "stalled"))
        self.assertEqual(stall_status(70.0, 71.0, False), (70.0, None))

    def test_recovery_is_reported(self):
        self.assertEqual(stall_status(70.0, 71.0, True), (None, "resumed"))


if __name__ == "__main__":
    unittest.main()
