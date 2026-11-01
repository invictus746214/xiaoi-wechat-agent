import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

from bridge import WeChatUI, active_view_changed, classify_bubble_positions, digest, find_wechat_application, handle_candidate, is_gray_bubble_pixel, preview_contains, same_selected_message, seed_initial_rows, unread_count, update_pending


class RuntimeTests(unittest.TestCase):
    def test_selects_only_the_wechat_process(self):
        apps = [SimpleNamespace(get_process_id=lambda: 11), SimpleNamespace(get_process_id=lambda: 22)]
        class Desktop:
            childCount = 2
            def __getitem__(self, i): return apps[i]
        self.assertIs(find_wechat_application(Desktop(), 22), apps[1])

    def test_selected_chat_is_not_clicked_again(self):
        clicks = []
        row = SimpleNamespace(path="/row", getState=lambda: SimpleNamespace(contains=lambda _: True))
        rect = SimpleNamespace(x=10, y=20, width=80, height=40)
        ui = WeChatUI.__new__(WeChatUI)
        ui.left_list = object()
        ui.rows = lambda _: [("contact\nhello", rect, row)]
        ui.window_active = lambda: True
        ui.pyatspi = SimpleNamespace(STATE_SELECTED=1)
        with patch("bridge.component_rect", return_value=SimpleNamespace(x=0, y=0, width=200, height=100)):
            self.assertTrue(ui.click_row(digest("contact\nhello")))
        self.assertEqual(clicks, [])

    def test_reused_preview_does_not_select_a_different_row_identity(self):
        clicks = []
        row = SimpleNamespace(path="/other", getState=lambda: SimpleNamespace(contains=lambda _: False))
        rect = SimpleNamespace(x=10, y=20, width=80, height=40)
        ui = WeChatUI.__new__(WeChatUI)
        ui.left_list = object()
        ui.rows = lambda _: [("contact\nhello", rect, row)]
        ui.window_active = lambda: True
        ui.pyatspi = SimpleNamespace(STATE_SELECTED=1)
        with patch("bridge.component_rect", return_value=SimpleNamespace(x=0, y=0, width=200, height=100)):
            result = ui.click_row(digest("contact\nhello"), digest("/wanted\ncontact"))
        self.assertIsNone(result)
        self.assertEqual(clicks, [])

    def test_bubble_colors_distinguish_messages_from_timestamps(self):
        gray = [(x, y) for x in range(66, 150, 3) for y in range(8, 44, 3)]
        green = [(x, y) for x in range(170, 300, 3) for y in range(8, 44, 3)]
        self.assertEqual(classify_bubble_positions(gray, [], 0), "incoming")
        self.assertEqual(classify_bubble_positions([], green, 0), "outgoing")
        self.assertEqual(classify_bubble_positions([(300, 15), (303, 18)], [], 0), "unknown")

    def test_bubble_color_must_differ_from_chat_background(self):
        self.assertTrue(is_gray_bubble_pixel((238, 238, 240), (250, 250, 250)))
        self.assertFalse(is_gray_bubble_pixel((238, 238, 240), (238, 238, 240)))

    def test_preview_accepts_punctuation_and_emoji(self):
        self.assertTrue(preview_contains("Alice\n？\n17:20", "？"))
        self.assertTrue(preview_contains("Alice\n🙂\n17:20", "🙂"))
        self.assertFalse(preview_contains("Alice\nhello", "Alice"))
        self.assertFalse(preview_contains("Alice\n不好\n17:20", "好"))
        self.assertFalse(preview_contains("Alice\nhello\n17:20", "17:20"))
        self.assertTrue(preview_contains("Group\n张三：hello\n17:20", "hello"))

    def test_unread_badge_does_not_hide_the_preview(self):
        self.assertEqual(unread_count("Alice\n[1]\nhello\n18:05"), 1)
        self.assertTrue(preview_contains("Alice\n[1]\nhello\n18:05", "hello"))
        self.assertEqual(unread_count("Alice\n[1]\n18:05"), 0)
        self.assertTrue(preview_contains("Alice\n[1]\n18:05", "[1]"))

    def test_startup_baselines_existing_unread_chats(self):
        row = SimpleNamespace(path="/unread")
        read = SimpleNamespace(path="/read")
        _, seen, pending = seed_initial_rows([("Alice\n[1]\nhello\n18:05", None, row), ("Bob\nhello\n18:05", None, read)])
        self.assertEqual(len(pending), 0)
        self.assertEqual(len(seen), 2)

    def test_poll_clears_accessibility_cache_before_reading_rows(self):
        class Container:
            childCount = 1
            cleared = False
            def clear_cache(self): self.cleared = True
            def __getitem__(self, _):
                return SimpleNamespace(name="fresh" if self.cleared else "stale", queryComponent=lambda: SimpleNamespace(getExtents=lambda _: SimpleNamespace(x=0, y=0, width=1, height=1)))
        ui = WeChatUI.__new__(WeChatUI)
        ui.pyatspi = SimpleNamespace(DESKTOP_COORDS=0)
        container = Container()
        self.assertEqual(ui.rows(container)[0][0], "fresh")
        self.assertTrue(container.cleared)

    def test_does_not_send_if_chat_changes_during_model_request(self):
        rect = SimpleNamespace(x=500, y=300, width=500, height=57)
        class FakeUI:
            right_list = object()
            current = "Alice\nhello"
            sent = False
            def click_row(self, _, expected_identity=None): return ("/same-row", "Alice")
            def _discover(self): pass
            def selected_row(self): return self.current
            def selected_info(self): return (self.current, "/same-row")
            def latest_message(self): return ("hello", rect, None)
            def incoming_state(self, *_): return "incoming"
            def rows(self, _): return [("hello", rect, None)]
            def window_active(self): return True
            def send(self, *_): self.sent = True; return True
        ui = FakeUI()
        def change_chat(*_):
            ui.current = "Bob\nhello"
            return "reply"
        with patch("bridge.time.sleep"), patch("bridge.deepseek_reply", side_effect=change_chat):
            handle_candidate(ui, digest(ui.current), deque(), set(), "test-key")
        self.assertFalse(ui.sent)

    def test_repeated_preview_is_detected_when_message_sequence_changes(self):
        self.assertTrue(active_view_changed(("chat", "old-sequence", "same-preview"), ("chat", "new-sequence", "same-preview")))
        self.assertFalse(active_view_changed(("chat-a", "old", "preview"), ("chat-b", "new", "preview")))

    def test_uncertain_direction_is_retried_without_calling_model(self):
        rect = SimpleNamespace(x=500, y=300, width=500, height=57)
        class FakeUI:
            def click_row(self, _, expected_identity=None): return ("/row", "Alice")
            def _discover(self): pass
            def selected_info(self): return ("Alice\nhello", "/row")
            def latest_message(self): return ("hello", rect, None)
            def incoming_state(self, *_): return "unknown"
        with patch("bridge.time.sleep"), patch("bridge.deepseek_reply") as api:
            result = handle_candidate(FakeUI(), digest("Alice\nhello"), deque(), set(), "test-key")
        self.assertEqual(result, "retry")
        api.assert_not_called()

    def test_transient_failure_remains_pending_until_handled(self):
        pending = {"row:x": ("x", 0, 0.0, "identity")}
        seen, seen_set = deque(), set()
        for _ in range(5):
            update_pending(pending, "row:x", "retry", seen, seen_set, 100.0)
        self.assertEqual(pending["row:x"][1], 5)
        self.assertNotIn("row:x", seen_set)
        update_pending(pending, "row:x", "done", seen, seen_set, 100.0)
        self.assertNotIn("row:x", pending)
        self.assertIn("row:x", seen_set)

    def test_click_must_select_the_requested_chat(self):
        rect = SimpleNamespace(x=500, y=300, width=500, height=57)
        class FakeUI:
            right_list = object()
            def click_row(self, _, expected_identity=None): return ("/wanted", "Alice")
            def _discover(self): pass
            def selected_info(self): return ("Bob\nhello", "/other")
            def latest_message(self): return ("hello", rect, None)
            def incoming_state(self, *_): return "incoming"
            def rows(self, _): return [("hello", rect, None)]
        with patch("bridge.time.sleep"), patch("bridge.deepseek_reply") as api:
            result = handle_candidate(FakeUI(), "target", deque(), set(), "test-key")
        self.assertEqual(result, "retry")
        api.assert_not_called()

    def test_draft_preview_does_not_change_the_chat_identity(self):
        class FakeUI:
            def window_active(self): return True
            def selected_info(self): return ("Alice\n[draft]", "/row")
            def latest_message(self): return ("hello", None, None)
        self.assertTrue(same_selected_message(FakeUI(), (digest("Alice"), "/row", digest("hello"))))

    def test_send_requires_visible_outgoing_reply(self):
        class Composer:
            text = ""
            @property
            def characterCount(self): return len(self.text)
            def queryEditableText(self): return self
            def queryText(self): return self
            def insertText(self, _, value, __): self.text = value
            def getText(self, start, end): return self.text[start:end]
            def deleteText(self, start, end): self.text = self.text[:start] + self.text[end:]
        composer = Composer()
        ui = WeChatUI.__new__(WeChatUI)
        ui.composer = composer
        ui.send_rect = SimpleNamespace(x=0, y=0, width=10, height=10)
        ui.window_active = lambda: True
        ui.selected_info = lambda: ("Alice\nhello", "/row")
        ui.latest_message = lambda: ("hello", None, None)
        ui._discover = lambda: None
        ui._pointer = SimpleNamespace(tap=lambda *_: setattr(composer, "text", ""))
        with patch("bridge.time.sleep"):
            result = ui.send("reply", (digest("Alice"), "/row", digest("hello")))
        self.assertEqual(result, "uncertain")

    def test_unconfirmed_send_preserves_draft(self):
        class Composer:
            text = ""
            @property
            def characterCount(self): return len(self.text)
            def queryEditableText(self): return self
            def queryText(self): return self
            def insertText(self, _, value, __): self.text = value
            def getText(self, start, end): return self.text[start:end]
            def deleteText(self, start, end): self.text = self.text[:start] + self.text[end:]
        composer = Composer()
        ui = WeChatUI.__new__(WeChatUI)
        ui.composer = composer
        ui.send_rect = SimpleNamespace(x=0, y=0, width=10, height=10)
        ui.window_active = lambda: True
        ui.selected_info = lambda: ("Alice\nhello", "/row")
        ui.latest_message = lambda: ("hello", None, None)
        ui._discover = lambda: None
        ui._pointer = SimpleNamespace(tap=lambda *_: None)
        with patch("bridge.time.sleep"):
            result = ui.send("reply", (digest("Alice"), "/row", digest("hello")))
        self.assertEqual(result, "uncertain")
        self.assertEqual(composer.text, "reply")

    def test_send_succeeds_when_outgoing_reply_is_visible(self):
        class Composer:
            text = ""
            @property
            def characterCount(self): return len(self.text)
            def queryEditableText(self): return self
            def queryText(self): return self
            def insertText(self, _, value, __): self.text = value
            def getText(self, start, end): return self.text[start:end]
            def deleteText(self, start, end): self.text = self.text[:start] + self.text[end:]
        composer = Composer()
        ui = WeChatUI.__new__(WeChatUI)
        ui.composer = composer
        ui.send_rect = SimpleNamespace(x=0, y=0, width=10, height=10)
        ui.window_active = lambda: True
        ui.selected_info = lambda: ("Alice\nhello", "/row")
        ui.latest = ("hello", None, None)
        ui.latest_message = lambda: ui.latest
        ui.incoming_state = lambda *_: "outgoing"
        ui._discover = lambda: None
        def click(*_):
            composer.text = ""
            ui.latest = ("reply", None, None)
        ui._pointer = SimpleNamespace(tap=click)
        with patch("bridge.time.sleep"):
            result = ui.send("reply", (digest("Alice"), "/row", digest("hello")))
        self.assertEqual(result, "sent")

    def test_send_retries_click_only_when_same_draft_is_still_present(self):
        class Composer:
            text = ""
            @property
            def characterCount(self): return len(self.text)
            def queryEditableText(self): return self
            def queryText(self): return self
            def insertText(self, _, value, __): self.text = value
            def getText(self, start, end): return self.text[start:end]
            def deleteText(self, start, end): self.text = self.text[:start] + self.text[end:]
        composer = Composer()
        ui = WeChatUI.__new__(WeChatUI)
        ui.composer = composer
        ui.send_rect = SimpleNamespace(x=0, y=0, width=10, height=10)
        ui.window_active = lambda: True
        ui.selected_info = lambda: ("Alice\nhello", "/row")
        ui.latest = ("hello", None, None)
        ui.latest_message = lambda: ui.latest
        ui.incoming_state = lambda *_: "outgoing"
        ui._discover = lambda: None
        clicks = []
        def click(*_):
            clicks.append(1)
            if len(clicks) == 2:
                composer.text = ""
                ui.latest = ("reply", None, None)
        ui._pointer = SimpleNamespace(tap=click)
        with patch("bridge.time.sleep"):
            result = ui.send("reply", (digest("Alice"), "/row", digest("hello")))
        self.assertEqual(result, "sent")
        self.assertEqual(len(clicks), 2)


if __name__ == "__main__":
    unittest.main()
