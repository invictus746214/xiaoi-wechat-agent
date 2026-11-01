import unittest

import actions


class FakeComposer:
    def __init__(self, value=""):
        self.value = value

    @property
    def characterCount(self):
        return len(self.value)

    def queryComponent(self):
        return self

    def getExtents(self, _coords):
        return Rect(10, 20, 400, 40)

    def queryEditableText(self):
        return self

    def queryText(self):
        return self

    def insertText(self, _offset, text, _length):
        self.value = text

    def getText(self, start, end):
        return self.value[start:end]

    def deleteText(self, start, end):
        self.value = self.value[:start] + self.value[end:]


class Rect:
    def __init__(self, x, y, width, height):
        self.x, self.y, self.width, self.height = x, y, width, height


class FakeUI:
    def __init__(self, composer=None):
        self.composer = composer
        self.send_rect = Rect(500, 600, 60, 30)
        self.pyatspi = type("P", (), {"DESKTOP_COORDS": 0})()


class ScriptedActions(actions.Actions):
    """Skip the real keyboard, keep the state machine."""

    def __init__(self, ui, cleared=True, typed=True, sent=True):
        super().__init__(ui, pointer=object(), pause=lambda _s: None)
        self.cleared = cleared
        self.typed = typed
        self.sent = sent
        self.calls = []

    def clear_composer(self, attempts=3):
        self.calls.append("clear")
        if self.cleared and self.ui.composer is not None:
            self.ui.composer.value = ""
        return self.cleared

    def type_message(self, text):
        self.calls.append(("type", text))
        if self.typed and self.ui.composer is not None:
            self.ui.composer.value = text
        return self.typed

    def click_send(self):
        self.calls.append("send")
        if self.sent and self.ui.composer is not None:
            self.ui.composer.value = ""
        return self.sent

    def composer_empty(self):
        return self.ui.composer is not None and self.ui.composer.value == ""


class SendMessageTests(unittest.TestCase):
    def test_happy_path_reports_every_stage(self):
        ui = FakeUI(FakeComposer())
        report = ScriptedActions(ui).send_message("你好")
        self.assertEqual(report, {"cleared": True, "typed": True, "sent": True})

    def test_leftover_draft_is_cleared_before_typing(self):
        ui = FakeUI(FakeComposer("旧草稿"))
        actor = ScriptedActions(ui)
        report = actor.send_message("新消息")
        self.assertTrue(report["sent"])
        self.assertEqual(actor.calls[0], "clear")

    def test_failed_clear_stops_before_typing(self):
        ui = FakeUI(FakeComposer("卡住的草稿"))
        actor = ScriptedActions(ui, cleared=False)
        report = actor.send_message("新消息")
        self.assertFalse(report["cleared"])
        self.assertNotIn("send", actor.calls)
        self.assertEqual(ui.composer.value, "卡住的草稿")

    def test_failed_typing_does_not_send(self):
        ui = FakeUI(FakeComposer())
        actor = ScriptedActions(ui, typed=False)
        report = actor.send_message("你好")
        self.assertFalse(report["typed"])
        self.assertNotIn("send", actor.calls)

    def test_send_without_click_is_reported(self):
        ui = FakeUI(FakeComposer())
        report = ScriptedActions(ui, sent=False).send_message("你好")
        self.assertFalse(report["sent"])


class ComposerTests(unittest.TestCase):
    def test_missing_composer_is_reported(self):
        actor = actions.Actions(FakeUI(None), pointer=object())
        self.assertEqual(actor.composer_chars(), -1)
        self.assertFalse(actor.composer_empty())

    def test_reads_character_count(self):
        actor = actions.Actions(FakeUI(FakeComposer("abc")), pointer=object())
        self.assertEqual(actor.composer_chars(), 3)
        self.assertFalse(actor.composer_empty())


if __name__ == "__main__":
    unittest.main()
