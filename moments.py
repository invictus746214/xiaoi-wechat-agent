"""Publish a text-only WeChat Moment from the signed-in desktop client.

Why this file looks unusual:
- the text composer only opens on a *long press* of the camera button, and the
  synthetic clicks pyatspi emits are not recognised as a long press, so real
  X11 button events are sent through XTest instead;
- the dialog only appears in the accessibility tree while it is open, so every
  step is discovered at run time and any missing control aborts the post.
"""
from __future__ import annotations

import ctypes
import logging
import time

LOG = logging.getLogger("wechat-autoreply")

CAMERA_FALLBACK_OFFSET = (69, 21)
COMPOSE_MARKERS = ("Visible To", "Post", "Cancel")


class Pointer:
    """Real pointer events via XTest; a plain click is not enough for long press."""

    def __init__(self):
        self.x11 = ctypes.CDLL("libX11.so.6")
        self.xtst = ctypes.CDLL("libXtst.so.6")
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.display = ctypes.c_void_p(self.x11.XOpenDisplay(None))
        if not self.display:
            raise RuntimeError("cannot_open_display")
        self.xtst.XTestFakeMotionEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong,
        ]
        self.xtst.XTestFakeButtonEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong,
        ]
        self.x11.XFlush.argtypes = [ctypes.c_void_p]

    def move(self, x, y):
        self.xtst.XTestFakeMotionEvent(self.display, -1, int(x), int(y), 0)
        self.x11.XFlush(self.display)

    def tap(self, x, y):
        self.move(x, y)
        time.sleep(0.15)
        self.xtst.XTestFakeButtonEvent(self.display, 1, True, 0)
        self.x11.XFlush(self.display)
        time.sleep(0.08)
        self.xtst.XTestFakeButtonEvent(self.display, 1, False, 0)
        self.x11.XFlush(self.display)

    def long_press(self, x, y, hold=1.4):
        self.move(x, y)
        time.sleep(0.2)
        self.xtst.XTestFakeButtonEvent(self.display, 1, True, 0)
        self.x11.XFlush(self.display)
        time.sleep(hold)
        self.xtst.XTestFakeButtonEvent(self.display, 1, False, 0)
        self.x11.XFlush(self.display)

    def focus_window(self,title):
        if title not in ('Moments','朋友圈'):
            return False
        x=self.x11;display=self.display
        x.XDefaultRootWindow.argtypes=[ctypes.c_void_p];x.XDefaultRootWindow.restype=ctypes.c_ulong
        x.XQueryTree.argtypes=[ctypes.c_void_p,ctypes.c_ulong,ctypes.POINTER(ctypes.c_ulong),ctypes.POINTER(ctypes.c_ulong),ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),ctypes.POINTER(ctypes.c_uint)]
        x.XFetchName.argtypes=[ctypes.c_void_p,ctypes.c_ulong,ctypes.POINTER(ctypes.c_char_p)]
        x.XRaiseWindow.argtypes=[ctypes.c_void_p,ctypes.c_ulong]
        x.XSetInputFocus.argtypes=[ctypes.c_void_p,ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong]
        x.XGetInputFocus.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong),ctypes.POINTER(ctypes.c_int)]
        x.XFree.argtypes=[ctypes.c_void_p]
        root=ctypes.c_ulong();parent=ctypes.c_ulong();children=ctypes.POINTER(ctypes.c_ulong)();count=ctypes.c_uint()
        if not x.XQueryTree(display,x.XDefaultRootWindow(display),ctypes.byref(root),ctypes.byref(parent),ctypes.byref(children),ctypes.byref(count)):
            return False
        found=[]
        try:
            for index in range(count.value):
                name=ctypes.c_char_p()
                if x.XFetchName(display,children[index],ctypes.byref(name)) and name.value:
                    if name.value.decode('utf-8','replace') in ('Moments','朋友圈'):
                        found.append(children[index])
                    x.XFree(name)
        finally:
            if children:x.XFree(children)
        if len(found)!=1:return False
        x.XRaiseWindow(display,found[0]);x.XSetInputFocus(display,found[0],2,0);x.XFlush(display)
        focus=ctypes.c_ulong();revert=ctypes.c_int()
        x.XGetInputFocus(display,ctypes.byref(focus),ctypes.byref(revert))
        return focus.value==found[0]

    def scroll(self, steps=3, up=False):
        """Wheel scroll (XTest buttons 4/5). Steps are discrete wheel clicks."""
        button = 4 if up else 5
        for _ in range(max(1, int(steps))):
            self.xtst.XTestFakeButtonEvent(self.display, button, True, 0)
            self.xtst.XTestFakeButtonEvent(self.display, button, False, 0)
            self.x11.XFlush(self.display)
            time.sleep(0.08)

    def type_text(self, text, keycode=255):
        """Type arbitrary Unicode by remapping a spare keycode (xdotool's trick)."""
        if not text:
            return False
        self.x11.XChangeKeyboardMapping.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_ulong), ctypes.c_int,
        ]
        self.x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.xtst.XTestFakeKeyEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong,
        ]
        try:
            for char in text:
                mapping = (ctypes.c_ulong * 1)(0x01000000 + ord(char))
                self.x11.XChangeKeyboardMapping(self.display, keycode, 1, mapping, 1)
                self.x11.XSync(self.display, False)
                time.sleep(0.03)
                self.xtst.XTestFakeKeyEvent(self.display, keycode, True, 0)
                self.xtst.XTestFakeKeyEvent(self.display, keycode, False, 0)
                self.x11.XSync(self.display, False)
                time.sleep(0.05)
        finally:
            empty = (ctypes.c_ulong * 1)(0)
            self.x11.XChangeKeyboardMapping(self.display, keycode, 1, empty, 1)
            self.x11.XSync(self.display, False)
        return True

    def key(self, name, ctrl=False):
        """Send one real key press (optionally with Ctrl held)."""
        self.x11.XStringToKeysym.restype = ctypes.c_ulong
        self.x11.XKeysymToKeycode.restype = ctypes.c_ubyte
        self.xtst.XTestFakeKeyEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong,
        ]
        code = int(self.x11.XKeysymToKeycode(self.display, self.x11.XStringToKeysym(name.encode())))
        if not code:
            return False
        control = 0
        if ctrl:
            control = int(self.x11.XKeysymToKeycode(self.display, self.x11.XStringToKeysym(b"Control_L")))
            if control:
                self.xtst.XTestFakeKeyEvent(self.display, control, True, 0)
        self.xtst.XTestFakeKeyEvent(self.display, code, True, 0)
        self.xtst.XTestFakeKeyEvent(self.display, code, False, 0)
        if control:
            self.xtst.XTestFakeKeyEvent(self.display, control, False, 0)
        self.x11.XFlush(self.display)
        return True


def _rect(obj, pyatspi):
    return obj.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)


def _name(obj):
    try:
        return (obj.name or "").strip()
    except Exception:
        return ""


def walk(root, limit=12000):
    stack, seen = [root], 0
    while stack and seen < limit:
        obj = stack.pop()
        seen += 1
        yield obj
        try:
            for index in range(min(obj.childCount, 70)):
                stack.append(obj[index])
        except Exception:
            pass


def find(root, predicate, limit=8000):
    for obj in walk(root, limit):
        try:
            if predicate(obj):
                return obj
        except Exception:
            pass
    return None


def buttons(root, label):
    return [obj for obj in walk(root) if _name(obj) == label and _role(obj) == "push button"]


def _role(obj):
    try:
        return obj.getRoleName()
    except Exception:
        return ""


class MomentPoster:
    def __init__(self, ui, pointer=None, pause=time.sleep):
        self.ui = ui
        self.pointer = pointer or Pointer()
        self.pause = pause

    def rect(self, obj):
        return _rect(obj, self.ui.pyatspi)

    def showing(self, obj):
        try:
            return bool(obj.getState().contains(self.ui.pyatspi.STATE_SHOWING))
        except Exception:
            return False

    def buttons(self, label):
        """Only visible buttons count: hidden leftovers break every click."""
        out = []
        for obj in walk(self.ui.app):
            if _name(obj) != label or _role(obj) != "push button":
                continue
            if not self.showing(obj):
                continue
            out.append(obj)
        return out

    def nearest(self, label, anchor):
        candidates = self.buttons(label)
        if not candidates:
            return None
        ax, ay = anchor
        return min(
            candidates,
            key=lambda obj: (
                (self.rect(obj).x + self.rect(obj).width // 2 - ax) ** 2
                + (self.rect(obj).y + self.rect(obj).height // 2 - ay) ** 2
            ),
        )

    # -- discovery --------------------------------------------------------
    def feed(self):
        for index in range(self.ui.app.childCount):
            try:
                child = self.ui.app[index]
                if _role(child) == "frame" and _name(child) in ("Moments", "朋友圈"):
                    return child
            except Exception:
                continue
        return None

    def compose_open(self):
        return bool(self.buttons("Visible To"))

    def open_feed(self):
        if self.feed() is not None:
            return self.activate_feed()
        activate=getattr(self.ui,'activate',None)
        if activate and not activate():return False
        entry = find(self.ui.app, lambda o: _name(o) in ("Moments", "朋友圈") and _role(o) == "push button" and self.showing(o), 4000)
        if entry is None:
            return False
        rect = self.rect(entry)
        self.pointer.tap(rect.x + rect.width // 2, rect.y + rect.height // 2)
        self.pause(3.5)
        return self.activate_feed()

    def activate_feed(self):
        if self.feed() is None:return False
        focus=getattr(self.pointer,'focus_window',None)
        if focus and not focus('Moments'):return False
        self.pause(0.2)
        try:self.ui.app.clear_cache()
        except AttributeError:pass
        return True

    def camera_point(self):
        frame = self.feed()
        if frame is None:
            return None
        button = find(frame, lambda o: _role(o) == "push button" and _name(o) == "Post", 1200)
        if button is not None:
            rect = self.rect(button)
            return rect.x + rect.width // 2, rect.y + rect.height // 2
        rect = self.rect(frame)
        if rect.width and rect.height:
            return rect.x + CAMERA_FALLBACK_OFFSET[0], rect.y + CAMERA_FALLBACK_OFFSET[1]
        return None

    # -- actions ----------------------------------------------------------
    def open_text_composer(self):
        point = self.camera_point()
        if point is None:
            return False
        self.pointer.long_press(point[0], point[1])
        self.pause(1.5)
        return self.compose_open()

    def caption_field(self):
        for obj in walk(self.ui.app, 6000):
            try:
                if _role(obj) != "text":
                    continue
                obj.queryEditableText()
                return obj
            except Exception:
                continue
        return None

    def clear_caption(self):
        field = self.caption_field()
        if field is None:
            return False
        try:
            text = field.queryText()
            count = text.characterCount
            if count:
                field.queryEditableText().deleteText(0, count)
                self.pause(0.2)
            return True
        except Exception:
            return False

    def write_caption(self, text):
        field = self.caption_field()
        if field is None:
            return False
        rect = self.rect(field)
        self.pointer.tap(rect.x + 20, rect.y + 12)
        self.pause(0.3)
        self.clear_caption()
        field.queryEditableText().insertText(0, text, len(text))
        self.pause(0.3)
        return field.queryText().characterCount > 0

    def discard(self):
        visible_to = self.buttons("Visible To")
        if visible_to:
            # Clear the caption first: an empty draft closes without prompting.
            self.clear_caption()
            anchor_rect = self.rect(visible_to[0])
            anchor = (anchor_rect.x + anchor_rect.width // 2, anchor_rect.y + anchor_rect.height // 2)
            cancel = self.nearest("Cancel", anchor)
            if cancel is not None:
                rect = self.rect(cancel)
                self.pointer.tap(rect.x + rect.width // 2, rect.y + rect.height // 2)
                self.pause(1.0)
        # "Save this draft?" shows up about a second later, so poll for it.
        for _ in range(12):
            discard = self.buttons("Discard")
            if discard:
                rect = self.rect(discard[0])
                self.pointer.tap(rect.x + rect.width // 2, rect.y + rect.height // 2)
                self.pause(0.8)
                break
            self.pause(0.25)
        return not self.compose_open()

    def publish(self):
        visible_to = self.buttons("Visible To")
        if not visible_to:
            return False
        anchor_rect = self.rect(visible_to[0])
        anchor = (anchor_rect.x + anchor_rect.width // 2, anchor_rect.y + anchor_rect.height // 2)
        post = self.nearest("Post", anchor)
        if post is None:
            return False
        rect = self.rect(post)
        self.pointer.tap(rect.x + rect.width // 2, rect.y + rect.height // 2)
        self.pause(2.5)
        return not self.compose_open()

    def refresh(self):
        buttons_ = self.buttons("Refresh")
        if not buttons_:
            return False
        rect = self.rect(buttons_[0])
        self.pointer.tap(rect.x + rect.width // 2, rect.y + rect.height // 2)
        self.pause(1.2)
        return True

    def verify(self, text, attempts=3):
        """A closed composer is not proof; look for the text in the feed."""
        needle = text.strip()[:12]
        if not needle:
            return False
        for _ in range(attempts):
            self.pause(0.8)
            for obj in walk(self.ui.app, 9000):
                if _role(obj) in ("list item", "label", "text") and needle in _name(obj):
                    return True
            self.refresh()
        return False

    def post_text(self, text, dry_run=True):
        """Return a stage report. Never publishes when dry_run is true."""
        report = {"opened_feed": False, "opened_composer": False, "wrote_caption": False,
                  "published": False, "verified": False, "discarded": False, "dry_run": dry_run}
        if self.compose_open():
            self.discard()
        report["opened_feed"] = self.open_feed()
        if not report["opened_feed"]:
            return report
        report["opened_composer"] = self.open_text_composer()
        if not report["opened_composer"]:
            return report
        report["wrote_caption"] = self.write_caption(text)
        if not report["wrote_caption"]:
            report["discarded"] = self.discard()
            return report
        if dry_run:
            report["discarded"] = self.discard()
            return report
        closed = self.publish()
        report["verified"] = self.verify(text) if closed else False
        report["published"] = bool(closed and report["verified"])
        if not report["published"]:
            report["discarded"] = self.discard()
        return report
