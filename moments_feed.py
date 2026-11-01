"""Read the Moments feed and like/comment on posts via the "..." menu.

The Linux client exposes posts as one ``list item`` whose name is
"author + text + time"; it exposes no per-post Like/Comment button. Both live
behind the small "..." control at the post footer, whose popup *is* accessible
(real push buttons named Like/Comment). So every action is:

    confirm post -> click "..." -> click menu item -> re-read to prove it

Nothing is reported as done unless the follow-up read confirms it.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from dataclasses import dataclass

from moments import MomentPoster, _name, _role, walk

LIKE_LABELS = ("Like", "赞")
UNLIKE_LABELS = ("Cancel", "Unlike", "取消", "取消赞")
COMMENT_LABELS = ("Comment", "评论")
REFRESH_OFFSET = (101, 25)
MENU_RADIUS = (520, 220)          # popup sits left/above the "..." control
SKIP_PREFIXES = ("comments", "comment", "评论", "余下", "like", "赞", "查看更多")

ITEM_ROLES = ("list item", "listitem", "article")
TEXT_ROLES = ("label", "text", "static", "paragraph")

MAX_TEXT = 800
MAX_AUTHOR = 24

TIME_SUFFIX = re.compile(
    r"\s+(?:\d+\s*(?:second|minute|hour|day|week|month|year)\(s\)\s+ago"
    r"|刚刚|\d+\s*(?:秒|分钟|小时|天|周|月)前)\s*$",
    re.IGNORECASE,
)


@dataclass
class Post:
    id: str
    author: str
    text: str
    rect: tuple
    index: int = 0
    liked: bool = False
    like_rect: tuple | None = None
    comment_rect: tuple | None = None

    def as_dict(self):
        return {
            "id": self.id,
            "author": self.author,
            "text": self.text,
            "liked": self.liked,
        }


def post_id(author, text):
    base = (text or "").splitlines()[0] if (text or "").strip() else ""
    raw = (author or "") + "\n" + base
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def split_name(name):
    """Split "author text time" into (author, text); author is best effort."""
    value = TIME_SUFFIX.sub("", (name or "").strip()).strip()
    if not value:
        return "", ""
    head, _, tail = value.partition(" ")
    if tail and len(head) <= MAX_AUTHOR and not head.endswith("：") and not head.endswith(":"):
        return head, tail
    return "", value


def menu_point(box):
    """The "..." sits just inside the post's bottom-right corner."""
    return box[0] + box[2] - 28, box[1] + box[3] - 8


def editor_box(pixels, width, height, stride, channels, origin, anchor_bottom):
    """Find the green outline around the self-drawn comment editor."""
    if channels < 3 or width < 200 or height < 100:
        return None
    def green(x, y):
        i = y * stride + x * channels
        r, g, b = pixels[i:i + 3]
        return g > 75 and g > r * 1.35 and g > b * 1.15
    edges = []
    # Scan the whole frame: on tall image/video posts the inline editor can sit
    # above the old midpoint, so starting at height//2 missed it.
    for y in range(60, height):
        start = None
        for x in range(20, width - 20):
            if green(x, y):
                if start is None:
                    start = x
            elif start is not None:
                if x - start >= 100:
                    edges.append((start, x, y))
                start = None
    for left, right, top in edges:
        for bottom_left, bottom_right, bottom in edges:
            if 40 <= bottom - top <= 180 and abs(left - bottom_left) <= 8 and abs(right - bottom_right) <= 8:
                mid = (top + bottom) // 2
                center = (left + right) // 2
                i = mid * stride + center * channels
                sides = all(any(green(x, mid) for x in range(max(0, edge - 8), min(width, edge + 9)))
                            for edge in (left, right))
                if sides and min(pixels[i:i + 3]) >= 185:
                    return origin[0] + left, origin[1] + top, right - left, bottom - top
    return None


def _box_to_tuple(rect):
    return (int(rect.x), int(rect.y), int(rect.width), int(rect.height))


class MomentsFeed:
    def __init__(self, ui, pointer=None, pause=time.sleep, ocr_reader=None):
        self.ui = ui
        self.pause = pause
        self.poster = MomentPoster(ui, pointer=pointer, pause=pause)
        self.pointer = self.poster.pointer
        self.ocr = ocr_reader

    # -- plumbing ---------------------------------------------------------
    def _box(self, obj):
        return _box_to_tuple(self.poster.rect(obj))

    def _visible(self, obj):
        return self.poster.showing(obj)

    def frame(self):
        return self.poster.feed()

    def frame_box(self):
        frame = self.poster.feed()
        return self._box(frame) if frame is not None else None

    def open(self):
        if self.poster.compose_open():
            return False
        frame = self.poster.feed()
        if frame is not None and not self.poster.activate_feed():
            return False
        if frame is not None and self._is_personal(frame):
            # A contact timeline / Details page is open: the patrol must use
            # the global feed, not whatever window was left behind.
            if not self.close():
                return False
            frame = None
        if frame is not None:
            return True
        return self.poster.open_feed()

    def _is_personal(self, frame):
        for obj in walk(frame, 4000):
            if _name(obj) == "Back" and _role(obj) == "push button" and self._visible(obj):
                box = self._box(obj)
                if box[2] and box[3]:
                    return True
        return False

    def close(self):
        frame = self.poster.feed()
        if frame is None:
            return True
        if not self.poster.activate_feed():
            return False
        for obj in walk(frame, 4000):
            if _name(obj) == "Close" and _role(obj) == "push button" and self._visible(obj):
                x, y = self._center(self._box(obj))
                self.pointer.tap(x, y)
                self.pause(0.8)
                break
        return self.poster.feed() is None

    def _center(self, box):
        return box[0] + box[2] // 2, box[1] + box[3] // 2

    def enter_comment(self, text, frame_box, anchor_box, placeholder=''):
        box = self._editor_box(frame_box, anchor_box)
        if box is None or self.ocr is None:
            return "not_typed"
        region = (box[0] + 5, box[1] + 5, box[2] - 10, max(10, box[3] - 40))
        def content(value):
            if not placeholder:return value
            flat=lambda s:re.sub(r'[^0-9A-Za-z\u4e00-\u9fff]','',s).casefold()
            probe=flat(placeholder);observed=flat(value or '')
            return observed[len(probe):] if probe and observed.startswith(probe) else value
        before = self._read_region(region)
        if self._contains(text, content(before)):
            return "uncertain"
        self.pointer.tap(box[0] + 20, box[1] + 20)
        self.pause(0.3)
        # A previous attempt can leave a draft in the self-drawn editor.
        self.pointer.key("a", ctrl=True)
        self.pointer.key("BackSpace")
        self.pause(0.2)
        if not self.pointer.type_text(text):
            return "not_typed"
        self.pause(0.6)
        after = self._read_region(region)
        return "typed" if self._contains(text, content(after)) else "uncertain"

    def _editor_box(self, frame_box, anchor_box):
        if not hasattr(self.ui, "Gdk") or not hasattr(self.ui, "screen"):
            return None
        x, y, width, height = frame_box
        for attempt in range(3):
            try:
                pixbuf = self.ui.Gdk.pixbuf_get_from_window(self.ui.screen, x, y, width, height)
                if pixbuf is not None:
                    box = editor_box(pixbuf.get_pixels(), pixbuf.get_width(), pixbuf.get_height(),
                                     pixbuf.get_rowstride(), pixbuf.get_n_channels(), (x, y),
                                     anchor_box[1] + anchor_box[3])
                    if box is not None:
                        return box
            except Exception:
                pass
            if attempt < 2:
                self.pause(0.4)
        return None

    def comment_send_point(self, frame_box, anchor_box):
        box = self._editor_box(frame_box, anchor_box)
        if box is None:
            return None
        return box[0] + box[2] - 50, box[1] + box[3] - 21

    # -- parsing ----------------------------------------------------------
    def _items(self, frame):
        items = []
        for obj in walk(frame, 20000):
            if _role(obj) not in ITEM_ROLES or not self._visible(obj):
                continue
            name = (_name(obj) or "").strip()
            if not name or name.casefold().startswith(SKIP_PREFIXES):
                continue
            box = self._box(obj)
            if box[2] < 150 or box[3] < 50:
                continue
            items.append((obj, box, name))
        items.sort(key=lambda row: (row[1][1], row[1][0]))
        kept = []
        for obj, box, name in items:
            if any(
                c[0] <= box[0] and c[1] <= box[1]
                and c[0] + c[2] >= box[0] + box[2] and c[1] + c[3] >= box[1] + box[3]
                for _, c, _ in kept
            ):
                continue
            kept.append((obj, box, name))
        return kept

    def _items_by_id(self, frame):
        out = {}
        for index, (obj, box, name) in enumerate(self._items(frame)):
            author, text = split_name(name)
            post = Post(
                id=post_id(author, text),
                author=author,
                text=text[:MAX_TEXT],
                rect=box,
                index=index,
            )
            out[post.id] = (post, obj, name)
        return out

    def posts(self, hover=False):
        frame = self.poster.feed()
        if frame is None:
            return []
        return [entry[0] for entry in self._items_by_id(frame).values()]

    def scan(self, pages=1, hover=True):
        found = {}
        for posts in self.iter_pages(pages=pages, hover=hover):
            for post in posts:
                found.setdefault(post.id, post)
        return list(found.values())

    def iter_pages(self, pages=1, hover=True):
        if not self.open():
            return
        for page in range(max(1, int(pages))):
            frame = self.poster.feed()
            if frame is None:
                break
            posts = self.posts(hover=hover)
            tries = 0
            while not posts and tries < 3:
                self.pause(1.2)
                posts = self.posts(hover=hover)
                tries += 1
            yield posts
            if page + 1 < int(pages):
                box = self._box(frame)
                self.pointer.move(box[0] + box[2] // 2, box[1] + box[3] - 40)
                self.pointer.scroll(3)
                self.pause(1.2)

    # -- locate / scroll --------------------------------------------------
    def _locate(self, post):
        frame = self.poster.feed()
        if frame is None:
            return None
        entry = self._items_by_id(frame).get(post.id)
        return entry if entry is not None else None

    def _ensure_visible(self, entry):
        frame = self.poster.feed()
        if frame is None:
            return None
        fbox = self._box(frame)
        post, obj, name = entry
        for _ in range(4):
            box = self._box(obj)
            if fbox[1] - 2 <= box[1] and box[1] + box[3] <= fbox[1] + fbox[3] + 2:
                return entry
            self.pointer.move(fbox[0] + fbox[2] // 2, fbox[1] + fbox[3] - 40)
            self.pointer.scroll(3)
            self.pause(1.2)
            fresh = self._items_by_id(frame).get(post.id)
            if fresh is None:
                return None
            entry = fresh
            obj = entry[1]
        return None

    # -- menu -------------------------------------------------------------
    def _menu_buttons(self, x, y):
        found = {}
        for obj in walk(self.ui.app, 20000):
            if _role(obj) != "push button" or not self._visible(obj):
                continue
            label = _name(obj).strip()
            if label not in LIKE_LABELS + UNLIKE_LABELS + COMMENT_LABELS:
                continue
            box = self._box(obj)
            if box[2] <= 0 or box[3] <= 0:
                continue
            if abs(box[0] - x) <= MENU_RADIUS[0] and abs(box[1] - y) <= MENU_RADIUS[1]:
                found[label] = box
        return found

    def _open_menu(self, entry):
        box = self._box(entry[1])
        # The "..." is hover-revealed on some layouts; move over the post first.
        self.pointer.move(box[0] + box[2] // 2, box[1] + min(60, max(10, box[3] // 2)))
        self.pause(0.35)
        x, y = menu_point(box)
        buttons = self._menu_buttons(x, y)
        if buttons:
            return (x, y), buttons
        self.pointer.tap(x, y)
        self.pause(1.0)
        buttons = self._menu_buttons(x, y)
        if not buttons:
            # A leftover menu from an earlier attempt: this click just closed it.
            self.pointer.tap(x, y)
            self.pause(1.0)
            buttons = self._menu_buttons(x, y)
        return (x, y), buttons

    def _close_menu(self, point):
        if self._menu_buttons(point[0], point[1]):
            self.pointer.tap(point[0], point[1])
            self.pause(0.4)

    def _pick(self, buttons, labels):
        for label in labels:
            if label in buttons:
                return buttons[label]
        return None

    # -- actions ----------------------------------------------------------
    def like(self, post):
        entry = self._locate(post)
        if entry is None:
            return "missing"
        entry = self._ensure_visible(entry)
        if entry is None:
            return "missing"
        point, buttons = self._open_menu(entry)
        if self._pick(buttons, UNLIKE_LABELS):
            self._close_menu(point)
            return "already"
        target = self._pick(buttons, LIKE_LABELS)
        if target is None:
            self._close_menu(point)
            return "no_button"
        self.pointer.tap(*self._center(target))
        self.pause(1.3)
        point2, buttons2 = self._open_menu(entry)
        ok = bool(self._pick(buttons2, UNLIKE_LABELS))
        self._close_menu(point2)
        return "liked" if ok else "uncertain"

    def comment(self, post, text):
        text = (text or "").strip()[:100]
        if not text:
            return "empty"
        entry = self._locate(post)
        if entry is None:
            return "missing"
        entry = self._ensure_visible(entry)
        if entry is None:
            return "missing"
        point, buttons = self._open_menu(entry)
        target = self._pick(buttons, COMMENT_LABELS)
        if target is None:
            self._close_menu(point)
            return "no_button"
        anchor_box = target
        self.pointer.tap(*self._center(target))
        self.pause(1.2)
        fbox = self.frame_box()
        if fbox is None:
            return "missing"
        entered = self.enter_comment(text, fbox, anchor_box)
        if entered != "typed":
            return entered
        send_point = self.comment_send_point(fbox, anchor_box)
        if send_point is None:
            return "uncertain"
        self.pointer.tap(*send_point)
        # The editor closing after Send is the app's own success signal; the
        # comment can sit below the fold, so OCR of the feed is unreliable.
        for _ in range(6):
            self.pause(0.7)
            if self._editor_box(fbox, anchor_box) is None:
                return "commented"
        return "uncertain"

    def _comment_signature(self, post):
        """Footer rows change when a comment is added; that is a second signal."""
        frame = self.poster.feed()
        if frame is None:
            return ()
        rows = []
        for obj in walk(frame, 20000):
            name = (_name(obj) or "").strip().casefold()
            if not name.startswith(("comments", "comment", "评论")):
                continue
            box = self._box(obj)
            if post.rect[1] - 20 <= box[1] <= post.rect[1] + post.rect[3] + 320:
                rows.append((box[1], box[3]))
        return tuple(sorted(rows))

    def _read_region(self, region):
        if self.ocr is None:
            return ""
        handle, path = tempfile.mkstemp(prefix="moments-check-", suffix=".png")
        os.close(handle)
        try:
            if not self.ui.capture_region(region, path):
                return ""
            return self.ocr.read(path) or ""
        except Exception:
            return ""
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    @staticmethod
    def _contains(needle, haystack):
        probe = "".join(
            ch for ch in needle if re.match(r"[0-9A-Za-z\u4e00-\u9fff]", ch)
        )[:6]
        flat = "".join(
            ch for ch in (haystack or "") if re.match(r"[0-9A-Za-z\u4e00-\u9fff]", ch)
        )
        return bool(probe) and probe.casefold() in flat.casefold()

    def _reads(self, needle):
        """OCR the visible feed and look for the first few characters."""
        frame = self.poster.feed()
        return bool(frame is not None and self._contains(
            needle, self._read_region(self.poster.rect(frame))
        ))
