"""Moments feed reading and the "..." menu actions, without a real client."""
import unittest
from unittest.mock import patch

import bridge
import moments_feed


class Rect:
    def __init__(self, x, y, width, height):
        self.x, self.y, self.width, self.height = x, y, width, height


class Component:
    def __init__(self, rect):
        self.rect = rect

    def getExtents(self, _coords):
        return self.rect


class State:
    def contains(self, _state):
        return True


class Node:
    def __init__(self, name, role, rect, children=None):
        self.name = name
        self._role = role
        self._rect = rect
        self.children = list(children or [])
        self.childCount = len(self.children)

    def __getitem__(self, index):
        return self.children[index]

    def getRoleName(self):
        return self._role

    def queryComponent(self):
        return Component(self._rect)

    def getState(self):
        return State()


class Pyatspi:
    DESKTOP_COORDS = 0
    STATE_SHOWING = 0


class FakeUI:
    def __init__(self, app):
        self.app = app
        self.pyatspi = Pyatspi()
        self.shots = 0

    def capture_region(self, _rect, path):
        self.shots += 1
        with open(path, "wb") as target:
            target.write(b"png")
        return True


class FakeOcr:
    def __init__(self, pointer):
        self.pointer = pointer

    def read(self, _path):
        return self.pointer.typed


class FakePointer:
    def __init__(self, on_tap=None):
        self.taps = []
        self.moves = []
        self.keys = []
        self.typed = ""
        self.scrolls = 0
        self.on_tap = on_tap

    def tap(self, x, y):
        self.taps.append((x, y))
        if self.on_tap:
            self.on_tap(x, y)

    def move(self, x, y):
        self.moves.append((x, y))

    def scroll(self, steps=3, up=False):
        self.scrolls += steps

    def key(self, name, ctrl=False):
        self.keys.append((name, ctrl))
        return True

    def type_text(self, text):
        self.typed = text
        return True

    def long_press(self, x, y, hold=1.4):
        self.taps.append((x, y))


def inside(box, x, y):
    return box[0] <= x <= box[0] + box[2] and box[1] <= y <= box[1] + box[3]


class Harness:
    """A fake Moments window: one post plus a popup that really toggles."""

    def __init__(self, liked=False, with_ocr=True):
        self.liked = liked
        self.menu = []
        self.editor = False
        self.sent = False
        self.item = Node("张三 今天天气不错 1 hour(s) ago", "list item", Rect(10, 20, 400, 180))
        self.frame = Node("Moments", "frame", Rect(0, 0, 440, 560), children=[self.item])
        self.app = Node("", "application", Rect(0, 0, 1200, 800), children=[self.frame])
        self.ui = FakeUI(self.app)
        self.pointer = FakePointer(self.on_tap)
        self.poster_frame = self.frame
        self.dots = (10 + 400 - 28, 20 + 180 - 8)
        self.ocr = FakeOcr(self.pointer) if with_ocr else None
        self.feed = moments_feed.MomentsFeed(
            self.ui, pointer=self.pointer, pause=lambda _s: None, ocr_reader=self.ocr
        )
        self.feed._editor_box = lambda _frame, _anchor: (85, 452, 323, 98) if self.editor else None

    def open_menu(self):
        self.menu = []
        if self.liked:
            self.menu.append(Node("Cancel", "push button", Rect(250, 150, 80, 24)))
        else:
            self.menu.append(Node("Like", "push button", Rect(250, 150, 80, 24)))
        self.menu.append(Node("Comment", "push button", Rect(340, 150, 80, 24)))
        self.app.children = [self.frame] + self.menu
        self.app.childCount = len(self.app.children)

    def close_menu(self):
        self.menu = []
        self.app.children = [self.frame]
        self.app.childCount = 1

    def on_tap(self, x, y):
        if inside((self.dots[0], self.dots[1], 2, 2), x, y):
            if self.menu:
                self.close_menu()
            else:
                self.open_menu()
            return
        for node in list(self.menu):
            box = (node._rect.x, node._rect.y, node._rect.width, node._rect.height)
            if not inside(box, x, y):
                continue
            if node.name in ("Like", "Cancel"):
                self.close_menu()
                self.liked = node.name == "Like" or self.liked
            elif node.name == "Comment":
                self.close_menu()
                self.editor = True
            return
        if self.editor and abs(x - 361) <= 30 and abs(y - 535) <= 20:
            self.sent = True
            self.editor = False
            self.frame.children.append(Node("Comments", "list item", Rect(10, 210, 400, 60)))
            self.frame.childCount = len(self.frame.children)


class ParsingTests(unittest.TestCase):
    def test_recipient_placeholder_is_not_an_already_typed_reply(self):
        for name in ('invictus','Invictus'):
            harness=Harness()
            harness.editor=True
            harness.pointer.typed='Reply '+name
            result=harness.feed.enter_comment(name+' 谢谢你来看我',(0,0,440,560),(0,560,0,0),placeholder='Reply '+name)
            self.assertEqual(result,'typed')

    def test_unchanged_placeholder_never_counts_as_verified_typed_reply(self):
        harness=Harness();harness.editor=True
        harness.feed.ocr.read=lambda _: 'Reply invictus'
        result=harness.feed.enter_comment('invictus 谢谢你来看我',(0,0,440,560),(0,560,0,0),placeholder='Reply invictus')
        self.assertEqual(result,'uncertain')
        self.assertFalse(harness.sent)

    def test_editor_box_finds_outline_below_menu(self):
        width, height = 440, 560
        pixels = bytearray([255] * (width * height * 3))
        for y in (452, 550):
            for x in range(130, 370):
                pixels[(y * width + x) * 3:(y * width + x) * 3 + 3] = b"\x07\xc1\x60"
        for y in range(452, 551):
            for x in (130, 369):
                pixels[(y * width + x) * 3:(y * width + x) * 3 + 3] = b"\x07\xc1\x60"
        self.assertEqual((130, 452, 240, 98), moments_feed.editor_box(
            pixels, width, height, width * 3, 3, (0, 0), 174))
        self.assertEqual((130, 452, 240, 98), moments_feed.editor_box(
            pixels, width, height, width * 3, 3, (0, 0), 556))
        self.assertIsNone(moments_feed.editor_box(
            bytes([255] * len(pixels)), width, height, width * 3, 3, (0, 0), 174))

    def test_split_name(self):
        self.assertEqual(("张三", "今天天气不错"), moments_feed.split_name("张三 今天天气不错 1 hour(s) ago"))
        self.assertEqual(("SAD4NESS", "🐳 Video"), moments_feed.split_name("SAD4NESS 🐳 Video 7 hour(s) ago"))
        self.assertEqual(("", "只有内容"), moments_feed.split_name("只有内容"))

    def test_menu_point_is_bottom_right(self):
        self.assertEqual((382, 192), moments_feed.menu_point((10, 20, 400, 180)))

    def test_posts_read_author_text_and_skip_sections(self):
        harness = Harness()
        harness.frame.children.append(Node("Comments", "list item", Rect(10, 210, 400, 49)))
        harness.frame.children.append(Node("余下0条", "list item", Rect(10, 260, 400, 16)))
        harness.frame.childCount = len(harness.frame.children)
        posts = harness.feed.posts()
        self.assertEqual(1, len(posts))
        self.assertEqual("张三", posts[0].author)
        self.assertIn("今天", posts[0].text)

    def test_post_id_is_content_based(self):
        self.assertEqual(moments_feed.post_id("张三", "你好"), moments_feed.post_id("张三", "你好"))
        self.assertNotEqual(moments_feed.post_id("张三", "你好"), moments_feed.post_id("张三", "你好呀"))

    def test_pinned_posts_and_details_are_not_full_timeline(self):
        harness = Harness()
        with patch.object(bridge.moments, "Pointer", return_value=harness.pointer):
            self.assertIsNotNone(bridge.timeline_latest_post(harness.ui))
            header = Node("invictus's Pinned Posts", "label", Rect(20, 5, 250, 30))
            harness.frame.children.append(header)
            harness.frame.childCount = len(harness.frame.children)
            self.assertIsNone(bridge.timeline_latest_post(harness.ui))
            header.name = "Details"
            self.assertIsNone(bridge.timeline_latest_post(harness.ui))


class ActionTests(unittest.TestCase):
    def test_like_clicks_menu_and_confirms_cancel(self):
        harness = Harness(liked=False)
        post = harness.feed.posts()[0]
        self.assertEqual("liked", harness.feed.like(post))
        self.assertTrue(harness.liked)
        self.assertFalse(harness.menu)  # menu closed again

    def test_like_already(self):
        harness = Harness(liked=True)
        post = harness.feed.posts()[0]
        self.assertEqual("already", harness.feed.like(post))

    def test_like_missing_post(self):
        harness = Harness()
        ghost = moments_feed.Post("deadbeef", "张三", "旧内容", (0, 0, 1, 1))
        self.assertEqual("missing", harness.feed.like(ghost))

    def test_comment_types_sends_and_confirms(self):
        harness = Harness()
        post = harness.feed.posts()[0]
        self.assertEqual("commented", harness.feed.comment(post, "确实不错"))
        self.assertTrue(harness.sent)
        self.assertIn("确实不错", harness.pointer.typed)

    def test_comment_without_ocr_never_sends(self):
        harness = Harness(with_ocr=False)
        post = harness.feed.posts()[0]
        self.assertEqual("not_typed", harness.feed.comment(post, "确实不错"))
        self.assertFalse(harness.sent)

    def test_comment_draft_does_not_count_as_sent(self):
        harness = Harness()
        post = harness.feed.posts()[0]
        harness.pointer.on_tap = lambda x, y: harness.on_tap(x, y) if (x, y) != (358, 529) else None
        self.assertEqual("uncertain", harness.feed.comment(post, "确实不错"))
        self.assertFalse(harness.sent)

    def test_empty_comment_is_rejected(self):
        harness = Harness()
        post = harness.feed.posts()[0]
        self.assertEqual("empty", harness.feed.comment(post, "   "))
        self.assertFalse(harness.sent)

    def test_editor_box_above_midpoint_is_found(self):
        width, height = 440, 560
        pixels = bytearray([255]) * (width * height * 3)
        stride, channels = width * 3, 3

        def green(x, y):
            index = y * stride + x * channels
            pixels[index:index + 3] = b"\x00\xc8\x00"

        for x in range(50, 351):
            green(x, 100)
            green(x, 180)
        for y in range(100, 181):
            green(50, y)
            green(350, y)
        box = moments_feed.editor_box(bytes(pixels), width, height, stride, channels, (0, 0), height)
        self.assertIsNotNone(box)
        self.assertEqual(50, box[0])
        self.assertEqual(100, box[1])
        self.assertTrue(300 <= box[2] <= 302)
        self.assertEqual(80, box[3])

    def test_scan_needs_a_feed(self):
        app = Node("", "application", Rect(0, 0, 1200, 800))
        feed = moments_feed.MomentsFeed(FakeUI(app), pointer=FakePointer(), pause=lambda _s: None)
        self.assertEqual([], feed.scan())

    def test_personal_timeline_is_detected(self):
        harness = Harness()
        harness.frame.children.append(Node("Back", "push button", Rect(10, 10, 40, 28)))
        harness.frame.childCount = len(harness.frame.children)
        self.assertTrue(harness.feed._is_personal(harness.frame))


if __name__ == "__main__":
    unittest.main()
