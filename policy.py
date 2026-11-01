"""Reply policy: quiet hours, whitelist and group mention gating.

All checks are deny-by-default only for narrowing features that the user turned
on; with an empty config every private chat behaves exactly like before.
"""
from __future__ import annotations

import time


def _minutes(value):
    try:
        hour, minute = str(value).strip().split(":", 1)
        return int(hour) * 60 + int(minute)
    except (ValueError, AttributeError):
        return None


def in_quiet_hours(ranges, now=None):
    if not ranges:
        return False
    stamp = time.localtime(now if now is not None else time.time())
    current = stamp.tm_hour * 60 + stamp.tm_min
    for entry in ranges:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        start, end = _minutes(entry[0]), _minutes(entry[1])
        if start is None or end is None or start == end:
            continue
        if start < end:
            if start <= current < end:
                return True
        elif current >= start or current < end:
            return True
    return False


def allowed(policy, meta, now=None):
    """Return (allowed, reason). ``meta`` describes the conversation."""
    policy = policy or {}
    name = (meta or {}).get("name") or ""
    if name and name in (policy.get("skip_contacts") or []):
        return False, "skipped_contact"
    if in_quiet_hours(policy.get("quiet_hours") or [], now):
        return False, "quiet_hours"
    whitelist = policy.get("whitelist") or []
    if whitelist and name not in whitelist:
        return False, "not_whitelisted"
    if (meta or {}).get("kind") == "group" and name in (policy.get("group_always_reply") or []):
        return True, "ok"
    if (meta or {}).get("kind") == "group" and policy.get("group_mention_only"):
        active = ((meta or {}).get("is_mention") or (meta or {}).get("engaged")
                  or (meta or {}).get("interject")
                  or (meta or {}).get("group_reply_mode") in ("followup", "proactive", "engaged", "interject"))
        if not active:
            return False, "group_not_mentioned"
    return True, "ok"


def moment_allowed(moments, post, action, done, used_today, now=None):
    """Gate one Moments action. Deny-by-default: every switch must be on."""
    cfg = moments or {}
    if not cfg.get("enabled"):
        return False, "disabled"
    if action == "like" and not cfg.get("like_enabled"):
        return False, "like_disabled"
    if action == "comment" and not cfg.get("comment_enabled"):
        return False, "comment_disabled"
    author = (getattr(post, "author", "") or "").strip()
    if not author and ((cfg.get("authors") or []) or (cfg.get("skip_authors") or [])):
        # Author-scoped rules need a parsed name; without one we cannot judge.
        return False, "unknown_author"
    if author and author in (cfg.get("skip_authors") or []):
        return False, "author_skipped"
    text = (getattr(post, "text", "") or "").casefold()
    for keyword in cfg.get("skip_keywords") or []:
        if keyword.casefold() in text:
            return False, "keyword_skipped"
    authors = cfg.get("authors") or []
    if authors and author not in authors:
        return False, "author_not_allowed"
    if in_quiet_hours(cfg.get("quiet_hours") or [], now):
        return False, "quiet_hours"
    if done:
        return False, "already_done"
    cap = int(cfg.get(action + "_daily_cap") or 0)
    if cap and used_today >= cap:
        return False, "daily_cap"
    return True, "ok"
