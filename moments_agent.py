"""Plan and execute Moments likes/comments under policy.

The feed is untrusted input and the model is a suggestion engine; neither gets
to decide alone. Policy (allowlist, caps, quiet hours) gates every action, and
only a confirmed result is recorded, so a failed pass can be retried safely.
"""
from __future__ import annotations

import logging
from difflib import SequenceMatcher
import re
import time
from dataclasses import dataclass, field

import policy
from agent import ModelError

LOG = logging.getLogger("wechat-autoreply")

COMMENT_SYSTEM = """你在替微信号主人在朋友的朋友圈写一句评论。

要求：
- 一句，不超过 20 个汉字，口语化，像真人随手写的。
- 只针对这条内容，不提问、不客套、不喊口号、不堆 emoji。
- 不编造共同经历，不提钱，不评价外貌，不涉政治。
- 只输出评论本身，不要引号、前缀或解释。
"""

COMMENT_MAX = 40
URL = re.compile(r"https?://|www\.", re.IGNORECASE)


def clean_comment(text):
    """Reject quotes, line breaks, links and anything too long to be a comment."""
    value = (text or "").strip().strip("\"'“”‘’")
    value = value.splitlines()[0].strip() if value else ""
    if not value or len(value) > COMMENT_MAX or URL.search(value):
        return ""
    if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", value):
        return ""
    return value


def make_commenter(client, persona):
    """Return post -> comment text, or "" when the model has nothing safe."""

    def commenter(post):
        messages = [
            {"role": "system", "content": COMMENT_SYSTEM + "\n主人平时的说话风格：" + (persona or "")},
            {"role": "user", "content": "作者：" + post.author + "\n内容：" + (post.text or "")[:300]},
        ]
        try:
            result = client.chat(messages, None, 400)
        except ModelError as exc:
            LOG.warning("moment_comment_failed=%s", exc)
            return ""
        return clean_comment(result.get("content") or "")

    return commenter


REPLY_SYSTEM = """你在回复自己朋友圈帖子下面的一条评论。
- 只写一句自然、口语化的中文回复，不超过 40 字；
- 可以带上对方名字，不要解释，不要复述原评论；
- 不承诺钱、见面、合作，不透露隐私；
- 只输出回复正文。"""


def make_replier(client, persona):
    """Return (author, comment, post_text) -> short reply, or ""."""

    def replier(author, body, post_text):
        messages = [
            {"role": "system", "content": REPLY_SYSTEM + "\n平时的说话风格：" + (persona or "")},
            {"role": "user", "content": "帖子：" + (post_text or "")[:200] + "\n"
                                        + author + " 评论：" + (body or "")[:200]},
        ]
        try:
            result = client.chat(messages, None, 400)
        except ModelError as exc:
            LOG.warning("moment_reply_failed=%s", exc)
            return ""
        return clean_comment(result.get("content") or "")

    return replier


@dataclass
class Report:
    scanned: int = 0
    liked: list = field(default_factory=list)
    commented: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    dry_run: bool = True

    def as_dict(self):
        return {
            "scanned": self.scanned,
            "liked": list(self.liked),
            "commented": list(self.commented),
            "skipped": [list(item) for item in self.skipped],
            "dry_run": self.dry_run,
        }


class Runner:
    """One pass over the visible feed."""

    def __init__(self, feed, cfg, store=None, commenter=None, pause=time.sleep, clock=time.time, should_yield=None,reader=None):
        self.feed = feed
        self.cfg = cfg
        self.store = store
        self.commenter = commenter
        self.pause = pause
        self.clock = clock
        self.should_yield = should_yield or (lambda: False)
        self.reader=reader

    def native_target(self,post):
        names=self.reader.contacts()
        compact=lambda value: ''.join(c for c in value.casefold() if c.isalnum())
        text=compact(post.text)
        matches=[p for p in self.reader.moments(limit=100)
                 if names.get(p['username'],'').strip().casefold()==post.author.strip().casefold()
                 and text and (compact(p['text'])==text or (len(text)>=20 and compact(p['text']).startswith(text)))]
        return matches[0] if len(matches)==1 and matches[0]['username']!=self.reader.account else None

    def confirmed(self,target,action,text):
        before={c['id'] for c in target.get('thread',[]) if c['username']==self.reader.account}
        for _ in range(8):
            after=next((p for p in self.reader.moments(limit=100) if p['id']==target['id'] and p['username']==target['username']),None)
            if after and ((action=='like' and self.reader.account in after.get('likes',[])) or
                          (action=='comment' and any(c['username']==self.reader.account and c.get('id') and c['id'] not in before and not c.get('deleted') and c['text'].strip()==text.strip() for c in after.get('thread',[])))):
                return True
            self.pause(0.5)
        return False

    def run(self, dry_run=None, max_actions=None):
        moments = self.cfg["moments"]
        dry = moments["dry_run"] if dry_run is None else bool(dry_run)
        report = Report(dry_run=dry)
        posts = (post for page in self.feed.iter_pages(pages=moments["scan_pages"], hover=True)
                 for post in page)
        seen = set()
        used = {"like": self._used("like"), "comment": self._used("comment")}
        last_action = 0.0
        attempts = 0
        for post in posts:
            if self.should_yield():
                break
            if post.id in seen:
                continue
            if report.scanned >= moments["max_posts"]:
                break
            seen.add(post.id)
            report.scanned += 1
            for action in ("like", "comment"):
                if max_actions is not None and attempts >= max_actions:
                    return report
                if self.should_yield():
                    break
                done = bool(self.store and (
                    self.store.moment_done(post.id, action)
                    or self.store.moment_done(post.id, action + "_uncertain")
                    or self.store._rows("SELECT 1 FROM moment_activity WHERE post_id=? AND action=? AND state IN ('sending','uncertain','confirmed')",(post.id,action))
                ))
                allowed, reason = policy.moment_allowed(
                    moments, post, action, done, used[action], now=self.clock()
                )
                if not allowed:
                    report.skipped.append((post.id, action, reason))
                    continue
                if action=='comment' and not post.author.strip():
                    report.skipped.append((post.id,action,'unknown_author'))
                    continue
                if action=='comment' and self.store:
                    recent=self.store._rows("SELECT author,text,ts FROM moment_activity WHERE action='comment' AND state IN ('sending','uncertain','confirmed') AND ts>?",
                        (self.clock()-max(7,moments.get('comment_repeat_days',7))*86400,))
                    if any(r['author']==post.author.strip().casefold() and self.clock()-r['ts']<moments.get('comment_author_interval_seconds',21600) for r in recent):
                        report.skipped.append((post.id,action,'author_interval'))
                        continue
                text = ""
                if action == "comment":
                    text = self.commenter(post) if self.commenter else ""
                    if not text:
                        report.skipped.append((post.id, action, "empty_comment"))
                        continue
                    normalized=lambda value: ''.join(c for c in value.casefold() if c.isalnum())
                    if self.store and any(SequenceMatcher(None,normalized(text),normalized(r['text'])).ratio()>=0.85 for r in recent if r['ts']>=self.clock()-moments.get('comment_repeat_days',7)*86400):
                        report.skipped.append((post.id,action,'repeated_comment'))
                        continue
                if dry:
                    (report.liked if action == "like" else report.commented).append(post.id)
                    used[action] += 1
                    LOG.info("moment_dry_run action=%s post=%s", action, post.id)
                    continue
                gap = moments["min_interval_seconds"] - (self.clock() - last_action)
                if gap > 0:
                    if self.should_yield():
                        break
                    while gap > 0 and not self.should_yield():
                        step = min(0.25, gap)
                        self.pause(step)
                        gap -= step
                if self.should_yield():
                    break
                target=self.native_target(post) if self.reader else None
                if self.reader and target is None:
                    report.skipped.append((post.id,action,'native_target_unverified'))
                    continue
                if self.store:
                    self.store.reserve_moment(post,action,text,now=self.clock())
                attempts += 1
                try:
                    result = self.feed.like(post) if action == "like" else self.feed.comment(post, text)
                    if self.reader and result in ('liked','commented','already') and not self.confirmed(target,action,text):
                        result='uncertain'
                except Exception:
                    if self.store:
                        self.store.reserve_moment(post,action,text,'uncertain',now=self.clock())
                    raise
                if self.store:
                    self.store.reserve_moment(post,action,text,'confirmed' if result in ('liked','commented','already') else 'uncertain' if result=='uncertain' else 'failed',now=self.clock())
                last_action = self.clock()
                if result in ("liked", "commented"):
                    if self.store is not None:
                        self.store.mark_moment(post.id, action, now=self.clock())
                    (report.liked if action == "like" else report.commented).append(post.id)
                    used[action] += 1
                    LOG.info("moment_done action=%s post=%s", action, post.id)
                elif result == "already":
                    # Liked in the app but unknown to us: record it so the next
                    # pass does not reopen the menu for the same post.
                    if self.store is not None:
                        self.store.mark_moment(post.id, action, now=self.clock())
                    report.skipped.append((post.id, action, "already"))
                elif result == "uncertain":
                    # The click may have landed but we could not prove it. Never
                    # retry automatically: a second click could toggle a like off
                    # or duplicate a comment.
                    if self.store is not None:
                        self.store.mark_moment(post.id, action + "_uncertain", now=self.clock())
                    report.skipped.append((post.id, action, "failed_uncertain"))
                    used[action] += 1
                    LOG.warning("moment_uncertain action=%s post=%s", action, post.id)
                else:
                    report.skipped.append((post.id, action, "failed_" + result))
                    LOG.warning("moment_failed action=%s post=%s reason=%s", action, post.id, result)
        return report

    def _used(self, action):
        if self.store is None:
            return 0
        return self.store.moments_today(action, now=self.clock())
