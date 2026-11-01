import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

import bridge
from store import Store

RECT = SimpleNamespace(x=500, y=300, width=500, height=57)


class FakeUI:
    def __init__(self, selected="Alice\nhello", path="/row"):
        self.selected = selected
        self.path = path
        self.right_list = object()
        self.sent = []

    def click_row(self, _hash, expected_identity=None):
        return (self.path, self.selected.splitlines()[0])

    def _discover(self):
        pass

    def selected_info(self):
        return (self.selected, self.path)

    def latest_message(self):
        return (self.selected.splitlines()[-1], RECT, None)

    def incoming_state(self, *_):
        return "incoming"

    def rows(self, _container):
        return [(self.selected.splitlines()[-1], RECT, None)]

    def window_active(self):
        return True

    def send(self, reply, expected):
        self.sent.append(reply)
        return "sent"


class PrepareTests(unittest.TestCase):
    def test_private_chat_produces_a_job(self):
        with patch("bridge.time.sleep"):
            job, reason = bridge.prepare_candidate(FakeUI(), bridge.digest("Alice\nhello"))
        self.assertEqual(reason, "ok")
        self.assertEqual(job["kind"], "private")
        self.assertEqual(job["message"], "hello")
        self.assertGreaterEqual(len(job["session_key"]), 16)

    def test_group_chat_is_detected_from_the_sender_preview(self):
        with patch("bridge.time.sleep"):
            job, _ = bridge.prepare_candidate(FakeUI("同学群\n张三：hello"), bridge.digest("x"))
        self.assertEqual(job["kind"], "group")

    def test_media_rows_are_dropped_without_a_job(self):
        with patch("bridge.time.sleep"):
            job, reason = bridge.prepare_candidate(FakeUI("Alice\n[图片]"), bridge.digest("x"))
        self.assertIsNone(job)
        self.assertEqual(reason, "done")

    def test_chat_that_does_not_confirm_is_retried(self):
        ui = FakeUI()
        ui.selected_info = lambda: ("Bob\nhello", "/other")
        with patch("bridge.time.sleep"):
            job, reason = bridge.prepare_candidate(ui, bridge.digest("x"))
        self.assertIsNone(job)
        self.assertEqual(reason, "retry")

    def test_persisted_event_is_not_answered_twice(self):
        store = Store(":memory:")
        try:
            ui = FakeUI()
            with patch("bridge.time.sleep"):
                job, _ = bridge.prepare_candidate(ui, bridge.digest("x"), store=store)
                store.mark_event(job["event_id"])
                again, reason = bridge.prepare_candidate(ui, bridge.digest("x"), store=store)
            self.assertIsNone(again)
            self.assertEqual(reason, "done")
        finally:
            store.close()

    def test_unread_badge_change_keeps_the_same_event_id(self):
        with patch("bridge.time.sleep"):
            first = FakeUI("Alice\n[2]\nhello\n11:00")
            second = FakeUI("Alice\nhello\n11:00")
            first.latest_message = second.latest_message = lambda: ("hello", RECT, None)
            before, _ = bridge.prepare_candidate(first, bridge.digest("x"))
            after, _ = bridge.prepare_candidate(second, bridge.digest("x"))
        self.assertEqual(before["event_id"], after["event_id"])


    def test_lagging_preview_still_confirms_against_history(self):
        ui = FakeUI("Alice\nold\n18:05")
        ui.latest_message = lambda: ("new", RECT, None)
        ui.rows = lambda _container: [("old", RECT, None), ("new", RECT, None)]
        with patch("bridge.time.sleep"):
            job, reason = bridge.prepare_candidate(ui, bridge.digest("x"))
        self.assertEqual(reason, "ok")
        self.assertEqual(job["message"], "new")

    def test_unmatched_preview_falls_back_to_identity(self):
        ui = FakeUI("Alice\nold\n18:05")
        ui.latest_message = lambda: ("new", RECT, None)
        ui.rows = lambda _container: [("new", RECT, None)]
        with patch("bridge.time.sleep"):
            job, reason = bridge.prepare_candidate(ui, bridge.digest("x"))
        self.assertEqual(reason, "ok")
        self.assertEqual(job["message"], "new")

    def test_configured_contacts_are_skipped(self):
        ui = FakeUI()
        with patch("bridge.time.sleep"):
            job, reason = bridge.prepare_candidate(ui, bridge.digest("x"), skip_titles=["Alice"])
        self.assertIsNone(job)
        self.assertEqual(reason, "done")


class DeliverTests(unittest.TestCase):
    def test_uncertain_send_is_recorded_without_claiming_success_or_retrying(self):
        store = Store(":memory:")
        try:
            ui = FakeUI()
            ui.send = lambda *_: "uncertain"
            with patch("bridge.time.sleep"):
                job, _ = bridge.prepare_candidate(ui, bridge.digest("x"), store=store)
                result = bridge.deliver_reply(ui, job, "收到", deque(), set(), store)
            self.assertEqual("uncertain", result)
            self.assertTrue(store.seen_event(job["event_id"]))
            self.assertEqual([], store.recent(job["session_key"]))
            pending = {"row:key": ("hash", 0, 0, "identity")}
            bridge.update_pending(pending, "row:key", result, deque(), set(), 100.0)
            self.assertEqual({}, pending)
        finally:
            store.close()

    def test_reply_is_sent_into_the_same_chat(self):
        store = Store(":memory:")
        try:
            ui = FakeUI()
            with patch("bridge.time.sleep"):
                job, _ = bridge.prepare_candidate(ui, bridge.digest("x"), store=store)
                result = bridge.deliver_reply(ui, job, "收到", deque(), set(), store)
            self.assertEqual(result, "done")
            self.assertEqual(ui.sent, ["收到"])
            self.assertEqual(store.recent(job["session_key"]), [("user", "hello"), ("assistant", "收到")])
        finally:
            store.close()

    def test_chat_switch_during_the_model_call_blocks_the_send(self):
        ui = FakeUI()
        with patch("bridge.time.sleep"):
            job, _ = bridge.prepare_candidate(ui, bridge.digest("x"))
            ui.selected = "Bob\nhello"
            ui.path = "/other"
            result = bridge.deliver_reply(ui, job, "收到", deque(), set())
        self.assertEqual(result, "retry")
        self.assertEqual(ui.sent, [])


class HandleCandidateTests(unittest.TestCase):
    def test_responder_is_used_instead_of_the_legacy_client(self):
        ui = FakeUI()
        with patch("bridge.time.sleep"), patch("bridge.deepseek_reply") as legacy:
            result = bridge.handle_candidate(
                ui, bridge.digest("x"), deque(), set(), "key",
                responder=lambda job: "hi " + job["message"],
            )
        self.assertEqual(result, "done")
        legacy.assert_not_called()
        self.assertEqual(ui.sent, ["hi hello"])


if __name__ == "__main__":
    unittest.main()
