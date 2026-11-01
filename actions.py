"""Every UI mutation in one place: assert, act, verify.

The reply path grew organically; new features kept re-inventing "click here and
hope". This module is the seam: each operation states what it needs and proves
it happened, so callers only deal with business logic.
"""
from __future__ import annotations

import time

from moments import Pointer

SEND_WAIT_TICKS = 12
SEND_WAIT_SECONDS = 0.25


class Actions:
    def __init__(self, ui, pointer=None, pause=time.sleep):
        self.ui = ui
        self.pointer = pointer
        self.pause = pause

    # -- plumbing ---------------------------------------------------------
    def _pointer(self):
        if self.pointer is None:
            self.pointer = Pointer()
        return self.pointer

    def rect(self, obj):
        return obj.queryComponent().getExtents(self.ui.pyatspi.DESKTOP_COORDS)

    def composer_chars(self):
        if getattr(self.ui, "composer", None) is None:
            return -1
        try:
            return self.ui.composer.queryText().characterCount
        except Exception:
            return -1

    def composer_empty(self):
        return self.composer_chars() == 0

    # -- operations -------------------------------------------------------
    def clear_composer(self, attempts=3):
        if getattr(self.ui, "composer", None) is None:
            return False
        rect = self.rect(self.ui.composer)
        for _ in range(attempts):
            if self.composer_empty():
                return True
            self._pointer().tap(rect.x + rect.width // 2, rect.y + 16)
            self.pause(0.15)
            self._pointer().key("a", ctrl=True)
            self.pause(0.1)
            self._pointer().key("BackSpace")
            self.pause(0.35)
        return self.composer_empty()

    def type_message(self, text):
        if getattr(self.ui, "composer", None) is None:
            return False
        rect = self.rect(self.ui.composer)
        self._pointer().tap(rect.x + rect.width // 2, rect.y + 16)
        self.pause(0.15)
        self.ui.composer.queryEditableText().insertText(0, text, len(text))
        self.pause(0.2)
        current = self.ui.composer.queryText()
        return current.getText(0, current.characterCount) == text

    def click_send(self):
        if getattr(self.ui, "send_rect", None) is None:
            return False
        rect = self.ui.send_rect
        self._pointer().tap(rect.x + rect.width // 2, rect.y + rect.height // 2)
        return True

    def send_message(self, text):
        report = {"cleared": False, "typed": False, "sent": False}
        report["cleared"] = self.clear_composer()
        if not report["cleared"]:
            return report
        report["typed"] = self.type_message(text)
        if not report["typed"]:
            return report
        if not self.click_send():
            return report
        for _ in range(SEND_WAIT_TICKS):
            self.pause(SEND_WAIT_SECONDS)
            if self.composer_empty():
                report["sent"] = True
                break
        return report
