"""Optional side features: heartbeat alerting, scheduled greetings, Moments.

Everything here is off by default. ``post_moment`` is experimental: it only runs
when the config enables it *and* the probe actually finds a Moments entry in this
client's accessibility tree.
"""
from __future__ import annotations

import json
import logging
import time
from urllib.request import Request, urlopen

LOG = logging.getLogger("wechat-autoreply")

MOMENT_LABELS = ("朋友圈", "Moments", "moments")
MOMENT_BUTTONS = ("发表", "发布", "Post")
GREETING_WINDOW_SECONDS = 600


def heartbeat_due(last_sent, now, minutes):
    if not minutes:
        return False
    return last_sent is None or now - last_sent >= minutes * 60


def send_webhook(url, text, timeout=5):
    if not url:
        return False
    request = Request(
        url,
        data=json.dumps({"text": text}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout):
            return True
    except Exception:  # noqa: BLE001 - alerting must never break the loop
        LOG.warning("webhook_failed")
        return False


def _minutes_of_day(value):
    try:
        hour, minute = str(value).strip().split(":", 1)
        return int(hour) * 60 + int(minute)
    except (ValueError, AttributeError):
        return None


def due_greetings(greetings, now, sent_keys):
    """Return greetings whose time has arrived within the last ten minutes."""
    clock = time.localtime(now)
    current = clock.tm_hour * 60 + clock.tm_min + clock.tm_sec / 60.0
    date = time.strftime("%Y-%m-%d", clock)
    due = []
    for entry in greetings or []:
        target = _minutes_of_day(entry.get("at"))
        if target is None:
            continue
        if 0 <= current - target <= GREETING_WINDOW_SECONDS / 60.0:
            key = "%s %s %s" % (date, entry.get("at"), entry.get("contact"))
            if key in sent_keys:
                continue
            due.append((key, entry))
    return due


def probe_moments(app, limit=4000):
    """Look for a Moments entry in the accessibility tree."""
    stack, seen = [app], 0
    while stack and seen < limit:
        obj = stack.pop()
        seen += 1
        name, role = "", ""
        try:
            name = (obj.name or "").strip()
            role = obj.getRoleName()
        except Exception:  # noqa: BLE001
            pass
        if name in MOMENT_LABELS and role in ("push button", "menu item", "label", "page tab", "toggle button"):
            return {"name": name, "role": role}
        try:
            for index in range(min(obj.childCount, 120)):
                stack.append(obj[index])
        except Exception:  # noqa: BLE001
            continue
    return None


def post_moment(ui, text):
    """Best-effort text-only Moments post. Returns True only on visible success."""
    text = (text or "").strip()
    if not text:
        return False
    app = getattr(ui, "app", None)
    if app is None:
        return False
    if not probe_moments(app):
        LOG.warning("moments_entry_not_found")
        return False
    LOG.warning("moments_post_not_implemented")
    return False
