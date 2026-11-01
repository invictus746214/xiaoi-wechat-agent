"""SQLite state: persistent dedupe, conversation memory and usage counters.

Privacy choices:
- rows are keyed by a hash of the conversation identity, never by contact name;
- nothing is written to the system log, only to this local database;
- text is capped and pruned after ``retention_days``.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
from collections import Counter

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_key, id);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    fact TEXT NOT NULL,
    ts REAL NOT NULL,
    UNIQUE(session_key, fact)
);
CREATE TABLE IF NOT EXISTS summaries (
    session_key TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    through_id INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    day TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pending (
    key TEXT PRIMARY KEY,
    row_hash TEXT NOT NULL,
    identity TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    due_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS moments (
    post_id TEXT NOT NULL,
    action TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY(post_id, action)
);
CREATE TABLE IF NOT EXISTS vision_cache (
    image_hash TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS moment_activity (
    post_id TEXT NOT NULL,action TEXT NOT NULL,author TEXT NOT NULL,
    text TEXT NOT NULL,state TEXT NOT NULL,ts REAL NOT NULL,
    PRIMARY KEY(post_id,action)
);
CREATE TABLE IF NOT EXISTS outbox (
    delivery_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    event_id TEXT NOT NULL,
    message TEXT NOT NULL,
    reply TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_session ON outbox(session_key, updated_at);
CREATE INDEX IF NOT EXISTS outbox_state ON outbox(state);
CREATE TABLE IF NOT EXISTS delivery_memory (event_id TEXT PRIMARY KEY, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS summary_daily (
    session_key TEXT NOT NULL,
    day TEXT NOT NULL,
    text TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(session_key, day)
);
CREATE INDEX IF NOT EXISTS daily_session ON summary_daily(session_key, day);
CREATE TABLE IF NOT EXISTS session_cursor (
    session_key TEXT PRIMARY KEY,
    last_local_id INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS moment_interactions (
    hash TEXT PRIMARY KEY,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    post_text TEXT NOT NULL,
    action TEXT NOT NULL,
    ts REAL NOT NULL
);
"""

SELF_FACTS = (
    "我叫小i，也叫大肥鱼、小鱼；我的头像是蓝发女仆，抱着一只蓝鲸玩偶。",
    "我住在 Linux 微信客户端里，靠看屏幕和点按钮说话；会看错、会点空、会被重启。",
    "invictus（微信 ID Invictus-0511）是我唯一的老大。",
    "我会查天气、跨聊天传话、看谁找我、去朋友圈点赞评论。",
    "我不知道自己算不算有自我意识，但我知道自己是谁、在哪、记得聊过什么。",
)

STOPWORDS = {
    "今天", "昨天", "现在", "什么", "怎么", "可以", "帮我", "一下", "我们", "你们",
    "他们", "这个", "那个", "就是", "还是", "然后", "因为", "所以", "但是", "如果",
    "没有", "知道", "觉得", "真的", "一个", "不是", "自己", "对方", "现在", "已经",
}

MAX_TEXT = 2000
MAX_EVENTS = 5000


def session_key(value):
    """Stable, non-reversible key for a conversation identity."""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:32]


def today(now=None):
    return time.strftime("%Y-%m-%d", time.localtime(now if now is not None else time.time()))


def _bigrams(text):
    cleaned = re.sub(r"\s+", "", (text or "").casefold())
    if len(cleaned) < 2:
        return Counter([cleaned]) if cleaned else Counter()
    return Counter(cleaned[index:index + 2] for index in range(len(cleaned) - 1))


def _cosine(first, second):
    if not first or not second:
        return 0.0
    common = set(first) & set(second)
    if not common:
        return 0.0
    dot = sum(first[key] * second[key] for key in common)
    norm_first = sum(value * value for value in first.values()) ** 0.5
    norm_second = sum(value * value for value in second.values()) ** 0.5
    return dot / (norm_first * norm_second) if norm_first and norm_second else 0.0


class Store:
    def __init__(self, path=":memory:", retention_days=14):
        self.path = path
        self.retention_days = int(retention_days)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        for fact in SELF_FACTS:
            self.add_fact("__self__", fact)

    # -- plumbing ---------------------------------------------------------
    def add_delivery_memory(self, event_id, key, role, text):
        stamp = time.time()
        with self._lock, self._conn:
            added = self._conn.execute("INSERT OR IGNORE INTO delivery_memory VALUES(?,?)", (event_id, stamp)).rowcount
            if added:
                self._conn.execute("INSERT INTO messages(session_key,role,text,ts) VALUES(?,?,?,?)",
                                   (key, role, str(text)[:MAX_TEXT], stamp))

    def _rows(self, sql, params=()):
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def _write(self, sql, params=()):
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    # -- dedupe -----------------------------------------------------------
    def seen_event(self, event_id):
        return bool(self._rows("SELECT 1 FROM events WHERE event_id = ?", (event_id,)))

    def mark_event(self, event_id, now=None):
        stamp = now if now is not None else time.time()
        self._write("INSERT OR IGNORE INTO events(event_id, ts) VALUES (?, ?)", (event_id, stamp))
        self._write(
            "DELETE FROM events WHERE event_id NOT IN "
            "(SELECT event_id FROM events ORDER BY ts DESC LIMIT ?)",
            (MAX_EVENTS,),
        )

    # -- memory -----------------------------------------------------------
    def add_message(self, key, role, text, now=None):
        stamp = now if now is not None else time.time()
        self._write(
            "INSERT INTO messages(session_key, role, text, ts) VALUES (?, ?, ?, ?)",
            (key, role, str(text)[:MAX_TEXT], stamp),
        )

    def recent(self, key, limit=10):
        if limit <= 0:
            return []
        rows = self._rows(
            "SELECT role, text FROM (SELECT id, role, text FROM messages "
            "WHERE session_key = ? ORDER BY id DESC LIMIT ?) ORDER BY id",
            (key, int(limit)),
        )
        return [(row["role"], row["text"]) for row in rows]

    def last_message_time(self, key, role="assistant"):
        rows = self._rows("SELECT MAX(ts) AS ts FROM messages WHERE session_key = ? AND role = ?", (key, role))
        return rows[0]["ts"] if rows and rows[0]["ts"] is not None else 0.0

    def last_message_ts(self, key):
        rows = self._rows("SELECT MAX(ts) AS ts FROM messages WHERE session_key = ?", (key,))
        return float(rows[0]["ts"]) if rows and rows[0]["ts"] is not None else 0.0

    def cursor(self, key):
        rows = self._rows("SELECT last_local_id FROM session_cursor WHERE session_key = ?", (key,))
        return int(rows[0]["last_local_id"]) if rows else None

    def set_cursor(self, key, local_id, now=None):
        stamp = now if now is not None else time.time()
        self._write(
            "INSERT INTO session_cursor(session_key, last_local_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(session_key) DO UPDATE SET last_local_id = excluded.last_local_id, "
            "updated_at = excluded.updated_at",
            (key, int(local_id), stamp),
        )

    def count_events(self, prefix, since):
        rows = self._rows("SELECT COUNT(*) AS n FROM events WHERE event_id LIKE ? AND ts >= ?", (prefix + "%", since))
        return int(rows[0]["n"]) if rows else 0

    def cached_vision(self, image_hash):
        rows = self._rows("SELECT description FROM vision_cache WHERE image_hash = ?", (image_hash,))
        return rows[0]["description"] if rows else ""

    def cache_vision(self, image_hash, description, now=None):
        self._write("INSERT OR REPLACE INTO vision_cache(image_hash, description, ts) VALUES (?, ?, ?)",
                    (image_hash, str(description)[:300], now if now is not None else time.time()))

    def add_fact(self, key, fact, now=None):
        fact = str(fact).strip()[:200]
        if not fact:
            return False
        stamp = now if now is not None else time.time()
        cursor = self._write(
            "INSERT OR IGNORE INTO facts(session_key, fact, ts) VALUES (?, ?, ?)",
            (key, fact, stamp),
        )
        return bool(cursor.rowcount)

    def facts(self, key, limit=20):
        if limit <= 0:
            return []
        rows = self._rows(
            "SELECT fact FROM facts WHERE session_key = ? ORDER BY id DESC LIMIT ?",
            (key, int(limit)),
        )
        return [row["fact"] for row in rows][::-1]

    def self_facts(self, limit=20):
        return self.facts("__self__", limit)

    # -- memory retrieval ------------------------------------------------
    def search_memories(self, key, query, limit=6):
        """Local retrieval: keyword matches first, then char-bigram cosine.

        No embedding service is involved; this catches paraphrases that share
        characters (e.g. 深圳天气 -> 深圳明天有雨) without extra token cost.
        """
        terms = []
        for run in re.findall(r"[\u4e00-\u9fff]{2,}", query or ""):
            pieces = [run] if len(run) <= 3 else [run[i:i + 2] for i in range(len(run) - 1)]
            for term in pieces:
                if term in STOPWORDS or term in terms:
                    continue
                terms.append(term)
                if len(terms) >= 4:
                    break
            if len(terms) >= 4:
                break
        for term in re.findall(r"[A-Za-z0-9]{2,}", query or ""):
            if term.casefold() in STOPWORDS or term in terms:
                continue
            terms.append(term)
            if len(terms) >= 4:
                break

        rows = []
        if terms:
            clauses = " OR ".join("text LIKE ?" for _ in terms)
            params = [key] + ["%" + term + "%" for term in terms]
            rows = self._rows(
                "SELECT id, role, text FROM messages WHERE session_key = ? "
                "AND role IN ('user', 'assistant') AND (" + clauses + ") "
                "ORDER BY id DESC LIMIT 40",
                tuple(params),
            )

        seen, out = set(), []
        for row in rows:
            text = (row["text"] or "").strip()
            if text and text not in seen:
                seen.add(text)
                out.append((row["role"], text))
                if len(out) >= limit:
                    return list(reversed(out))

        query_vector = _bigrams(query)
        if not query_vector:
            return list(reversed(out))
        scored = []
        for row in self._rows(
            "SELECT id, role, text FROM messages WHERE session_key = ? "
            "AND role IN ('user', 'assistant') ORDER BY id DESC LIMIT 200",
            (key,),
        ):
            text = (row["text"] or "").strip()
            if not text or text in seen:
                continue
            score = _cosine(query_vector, _bigrams(text))
            if score >= 0.18:
                scored.append((score, row["role"], text))
        scored.sort(key=lambda item: item[0], reverse=True)
        for _score, role, text in scored:
            if len(out) >= limit:
                break
            seen.add(text)
            out.append((role, text))
        return list(reversed(out))

    # -- daily digest ----------------------------------------------------
    def set_daily_summary(self, key, day, text, now=None):
        stamp = now if now is not None else time.time()
        self._write(
            "INSERT INTO summary_daily(session_key, day, text, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(session_key, day) DO UPDATE SET text = excluded.text, "
            "updated_at = excluded.updated_at",
            (key, str(day), str(text)[:2000], stamp),
        )

    def daily_summary(self, key, day):
        rows = self._rows("SELECT text FROM summary_daily WHERE session_key = ? AND day = ?", (key, str(day)))
        return rows[0]["text"] if rows else ""

    def refresh_daily_digest(self, key, day=None, now=None):
        stamp = now if now is not None else time.time()
        day = day or today(stamp)
        try:
            start = time.mktime(time.strptime(day, "%Y-%m-%d"))
        except ValueError:
            return ""
        rows = self._rows(
            "SELECT role, text FROM messages WHERE session_key = ? AND ts >= ? AND ts < ? ORDER BY id",
            (key, start, start + 86400),
        )
        parts = []
        for row in rows[-40:]:
            who = "对方" if row["role"] == "user" else "我"
            text = (row["text"] or "").strip().replace("\n", " ")
            if text:
                parts.append(who + "：" + text[:160])
        digest = "\n".join(parts)[:1500]
        self.set_daily_summary(key, day, digest, now=stamp)
        return digest

    def sessions(self):
        return [row["session_key"] for row in self._rows("SELECT DISTINCT session_key FROM messages")]

    # -- Moments interactions (comments on her own posts) -----------------
    def interaction_seen(self, digest):
        return bool(self._rows("SELECT 1 FROM moment_interactions WHERE hash = ?", (digest,)))

    def mark_interaction(self, digest, author, body, post_text, action, now=None):
        stamp = now if now is not None else time.time()
        self._write(
            "INSERT OR IGNORE INTO moment_interactions(hash, author, body, post_text, action, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (digest, str(author)[:80], str(body)[:500], str(post_text)[:500], str(action), stamp),
        )

    def interaction_count(self):
        rows = self._rows("SELECT COUNT(*) AS n FROM moment_interactions")
        return int(rows[0]["n"]) if rows else 0

    # -- rolling summary --------------------------------------------------
    def get_summary(self, key):
        rows = self._rows("SELECT text, through_id FROM summaries WHERE session_key = ?", (key,))
        if not rows:
            return "", 0
        return rows[0]["text"], int(rows[0]["through_id"])

    def set_summary(self, key, text, through_id, now=None):
        stamp = now if now is not None else time.time()
        self._write(
            "INSERT INTO summaries(session_key, text, through_id, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(session_key) DO UPDATE SET text = excluded.text, "
            "through_id = excluded.through_id, updated_at = excluded.updated_at",
            (key, str(text)[:2000], int(through_id), stamp),
        )

    def messages_after(self, key, after_id=0, limit=200):
        rows = self._rows(
            "SELECT id, role, text FROM messages WHERE session_key = ? AND id > ? ORDER BY id LIMIT ?",
            (key, int(after_id), int(limit)),
        )
        return [(row["id"], row["role"], row["text"]) for row in rows]

    def max_message_id(self, key):
        rows = self._rows("SELECT MAX(id) AS top FROM messages WHERE session_key = ?", (key,))
        return int(rows[0]["top"] or 0) if rows else 0

    # -- usage ------------------------------------------------------------
    def add_usage(self, tokens_in=0, tokens_out=0, calls=1, now=None):
        day = today(now)
        self._write(
            "INSERT INTO usage(day, calls, tokens_in, tokens_out) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(day) DO UPDATE SET calls = calls + excluded.calls, "
            "tokens_in = tokens_in + excluded.tokens_in, "
            "tokens_out = tokens_out + excluded.tokens_out",
            (day, int(calls), int(tokens_in), int(tokens_out)),
        )

    def usage_today(self, now=None):
        rows = self._rows("SELECT calls, tokens_in, tokens_out FROM usage WHERE day = ?", (today(now),))
        if not rows:
            return {"calls": 0, "tokens_in": 0, "tokens_out": 0}
        return rows[0]

    # -- pending queue (survives a crash) ---------------------------------
    def save_pending(self, key, row_hash, identity, title="", created_at=None, attempts=0, due_at=0.0):
        stamp = created_at if created_at is not None else time.time()
        self._write(
            "INSERT INTO pending(key, row_hash, identity, title, created_at, attempts, due_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET attempts = excluded.attempts, due_at = excluded.due_at",
            (key, row_hash, identity, str(title)[:200], stamp, int(attempts), float(due_at)),
        )

    def delete_pending(self, key):
        self._write("DELETE FROM pending WHERE key = ?", (key,))

    def load_pending(self, since=None, limit=200):
        if since is None:
            rows = self._rows("SELECT * FROM pending ORDER BY created_at LIMIT ?", (int(limit),))
        else:
            rows = self._rows(
                "SELECT * FROM pending WHERE created_at >= ? ORDER BY created_at LIMIT ?",
                (float(since), int(limit)),
            )
        return rows

    def pending_count(self):
        rows = self._rows("SELECT COUNT(*) AS n FROM pending")
        return int(rows[0]["n"]) if rows else 0

    # -- moments (feed likes/comments) ------------------------------------
    def moment_done(self, post_id, action):
        return bool(self._rows(
            "SELECT 1 FROM moments WHERE post_id = ? AND action = ?", (str(post_id), str(action))
        ))

    def mark_moment(self, post_id, action, now=None):
        stamp = now if now is not None else time.time()
        self._write(
            "INSERT OR IGNORE INTO moments(post_id, action, ts) VALUES (?, ?, ?)",
            (str(post_id), str(action), stamp),
        )

    def moments_today(self, action, now=None):
        stamp = time.localtime(now if now is not None else time.time())
        start = time.mktime((stamp.tm_year, stamp.tm_mon, stamp.tm_mday, 0, 0, 0, 0, 0, -1))
        rows = self._rows(
            "SELECT COUNT(DISTINCT post_id) AS n FROM (SELECT post_id FROM moments WHERE action IN (?,?) AND ts>=? "
            "UNION SELECT post_id FROM moment_activity WHERE action=? AND state IN ('sending','uncertain','confirmed') AND ts>=?)",
            (str(action),str(action)+'_uncertain',start,str(action),start)
        )
        return int(rows[0]["n"]) if rows else 0

    def reserve_moment(self, post, action, text, state='sending', now=None):
        self._write('INSERT INTO moment_activity VALUES(?,?,?,?,?,?) ON CONFLICT(post_id,action) '
                    "DO UPDATE SET state=excluded.state,author=CASE WHEN moment_activity.state='failed' THEN excluded.author ELSE moment_activity.author END,"
                    "text=CASE WHEN moment_activity.state='failed' THEN excluded.text ELSE moment_activity.text END,"
                    "ts=CASE WHEN moment_activity.state='failed' THEN excluded.ts ELSE moment_activity.ts END",
                    (post.id,action,post.author.strip().casefold(),text,state,time.time() if now is None else now))

    # -- outbox (reply delivery state machine) ----------------------------
    def start_delivery(self, delivery_id, session_key, event_id, message, reply, now=None):
        stamp = now if now is not None else time.time()
        self._write(
            "INSERT OR IGNORE INTO outbox(delivery_id, session_key, event_id, message, reply, state, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
            (delivery_id, session_key, event_id, str(message)[:MAX_TEXT], str(reply)[:MAX_TEXT], stamp, stamp),
        )

    def set_delivery_state(self, delivery_id, state, now=None):
        stamp = now if now is not None else time.time()
        self._write(
            "UPDATE outbox SET state = ?, updated_at = ? WHERE delivery_id = ?",
            (str(state), stamp, delivery_id),
        )

    def delivery(self, delivery_id):
        rows = self._rows("SELECT * FROM outbox WHERE delivery_id = ?", (delivery_id,))
        return rows[0] if rows else None

    def delivery_count(self, session_key, since):
        rows = self._rows(
            "SELECT COUNT(*) AS n FROM outbox WHERE session_key = ? AND state = 'confirmed' AND updated_at >= ? "
            "AND NOT EXISTS(SELECT 1 FROM outbox child WHERE child.event_id LIKE outbox.event_id || ':part:%')",
            (session_key, float(since)),
        )
        return int(rows[0]["n"]) if rows else 0

    def delivery_times(self, session_key, since):
        rows = self._rows(
            "SELECT updated_at FROM outbox WHERE session_key = ? AND state = 'confirmed' AND updated_at >= ? "
            "AND NOT EXISTS(SELECT 1 FROM outbox child WHERE child.event_id LIKE outbox.event_id || ':part:%') ORDER BY updated_at",
            (session_key, float(since)),
        )
        return [float(row["updated_at"]) for row in rows]

    def last_delivery_at(self, session_key):
        rows = self._rows(
            "SELECT MAX(updated_at) AS ts FROM outbox WHERE session_key = ? AND state = 'confirmed' "
            "AND NOT EXISTS(SELECT 1 FROM outbox child WHERE child.event_id LIKE outbox.event_id || ':part:%')",
            (session_key,),
        )
        return float(rows[0]["ts"]) if rows and rows[0]["ts"] is not None else 0.0

    def recover_deliveries(self, now=None):
        """A crash mid-send is 'unconfirmed': never resend, but do not replay it."""
        stamp = now if now is not None else time.time()
        recovered = 0
        for row in self._rows("SELECT delivery_id, event_id, state FROM outbox WHERE state IN ('queued','sending')"):
            if row["state"] != "sending":
                continue
            self._write(
                "UPDATE outbox SET state = 'unconfirmed', updated_at = ? WHERE delivery_id = ?",
                (stamp, row["delivery_id"]),
            )
            self._write("INSERT OR IGNORE INTO events(event_id, ts) VALUES (?, ?)", (row["event_id"], stamp))
            recovered += 1
        return recovered

    # -- retention --------------------------------------------------------
    def purge(self, now=None):
        stamp = now if now is not None else time.time()
        cutoff = stamp - self.retention_days * 86400
        removed = 0
        for table in ("messages", "facts", "vision_cache", "delivery_memory"):
            cursor = self._write("DELETE FROM %s WHERE ts < ?" % table, (cutoff,))
            removed += cursor.rowcount
        cursor = self._write("DELETE FROM pending WHERE created_at < ?", (cutoff,))
        removed += cursor.rowcount
        cursor = self._write("DELETE FROM moments WHERE ts < ?", (cutoff,))
        removed += cursor.rowcount
        self._write("DELETE FROM moment_activity WHERE ts < ? AND state='failed'", (cutoff,))
        self._write("UPDATE moment_activity SET author='',text='' WHERE ts<?", (stamp-max(30,self.retention_days)*86400,))
        cursor = self._write("DELETE FROM outbox WHERE updated_at < ?", (cutoff,))
        removed += cursor.rowcount
        return removed

    def close(self):
        with self._lock:
            self._conn.close()
