import unittest

import moments


class RecordingPoster(moments.MomentPoster):
    """Drives the state machine without touching WeChat."""

    def __init__(self, feed=True, composer=True, caption=True, publish_ok=True,
                 compose_already_open=False, verified=True):
        self.feed_ok = feed
        self.composer_ok = composer
        self.caption_ok = caption
        self.publish_ok = publish_ok
        self.verified = verified
        self._open = compose_already_open
        self.calls = []

    def compose_open(self):
        return self._open

    def open_feed(self):
        self.calls.append("open_feed")
        return self.feed_ok

    def open_text_composer(self):
        self.calls.append("open_text_composer")
        self._open = self.composer_ok
        return self.composer_ok

    def write_caption(self, text):
        self.calls.append(("write_caption", text))
        return self.caption_ok

    def publish(self):
        self.calls.append("publish")
        self._open = not self.publish_ok
        return self.publish_ok

    def verify(self, text, attempts=3):
        self.calls.append("verify")
        return self.verified

    def discard(self):
        self.calls.append("discard")
        self._open = False
        return True


class PostTextTests(unittest.TestCase):
    def test_dry_run_never_publishes(self):
        poster = RecordingPoster()
        report = poster.post_text("今天天气不错", dry_run=True)
        self.assertEqual(poster.calls, ["open_feed", "open_text_composer", ("write_caption", "今天天气不错"), "discard"])
        self.assertFalse(report["published"])
        self.assertTrue(report["discarded"])

    def test_publish_path_sends_the_post(self):
        poster = RecordingPoster()
        report = poster.post_text("晚安", dry_run=False)
        self.assertTrue(report["published"])
        self.assertTrue(report["verified"])
        self.assertIn("publish", poster.calls)

    def test_closed_window_without_visible_post_is_not_success(self):
        poster = RecordingPoster(verified=False)
        report = poster.post_text("晚安", dry_run=False)
        self.assertFalse(report["published"])
        self.assertFalse(report["verified"])
        self.assertIn("discard", poster.calls)

    def test_missing_feed_aborts_before_typing(self):
        poster = RecordingPoster(feed=False)
        report = poster.post_text("x", dry_run=False)
        self.assertFalse(report["opened_feed"])
        self.assertEqual(poster.calls, ["open_feed"])

    def test_missing_composer_aborts_before_typing(self):
        poster = RecordingPoster(composer=False)
        report = poster.post_text("x", dry_run=False)
        self.assertFalse(report["opened_composer"])
        self.assertNotIn("publish", poster.calls)

    def test_caption_failure_discards_instead_of_publishing(self):
        poster = RecordingPoster(caption=False)
        report = poster.post_text("x", dry_run=False)
        self.assertFalse(report["published"])
        self.assertTrue(report["discarded"])
        self.assertNotIn("publish", poster.calls)

    def test_stale_composer_is_discarded_first(self):
        poster = RecordingPoster(compose_already_open=True)
        poster.post_text("x", dry_run=True)
        self.assertEqual(poster.calls[0], "discard")


if __name__ == "__main__":
    unittest.main()
