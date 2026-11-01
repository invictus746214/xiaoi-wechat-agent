"""Durable database inbox; no GUI calls are permitted in the reader thread."""
import hashlib
import json
import logging
import sqlite3
import threading
import time

LOG = logging.getLogger("wechat-autoreply")


class Inbox:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, timeout=20, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS inbox_cursor (
                account TEXT, username TEXT, source TEXT, local_id INTEGER NOT NULL,
                minimum_timestamp REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(account, username, source));
            CREATE TABLE IF NOT EXISTS inbox (
                event_id TEXT PRIMARY KEY, account TEXT NOT NULL, username TEXT NOT NULL,
                source TEXT NOT NULL, local_id INTEGER NOT NULL, title TEXT NOT NULL,
                payload TEXT NOT NULL, priority INTEGER NOT NULL, ts REAL NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                due REAL NOT NULL DEFAULT 0, batch_id TEXT, reply TEXT,
                UNIQUE(account, username, source, local_id));
            CREATE INDEX IF NOT EXISTS inbox_ready ON inbox(state, due, priority, ts);
            CREATE TABLE IF NOT EXISTS conversation_alias (
                account TEXT, username TEXT, memory_key TEXT NOT NULL,
                PRIMARY KEY(account, username));
            CREATE TABLE IF NOT EXISTS inbox_bootstrap(account TEXT PRIMARY KEY, started REAL NOT NULL);
        """)

    def cursor(self, account, username, source):
        with self.lock:
            row = self.db.execute("SELECT local_id FROM inbox_cursor WHERE account=? AND username=? AND source=?",
                                  (account, username, source)).fetchone()
            return row[0] if row else None

    def ingest(self, account, username, source, title, messages, priority, baseline=None, minimum_timestamp=0):
        # Persist messages and read position in the same transaction, including outgoing rows.
        with self.lock, self.db:
            cursor = self.cursor(account, username, source)
            cursor = baseline if cursor is None else cursor
            cursor = 0 if cursor is None else cursor
            inserted = 0
            for message in messages:
                local = int(message["local_id"])
                if local <= cursor:
                    continue
                payload = json.dumps(message, ensure_ascii=False)
                if not message.get("outgoing") and message.get("timestamp", 0) >= minimum_timestamp:
                    event = hashlib.sha256(f"{account}\0{username}\0{source}\0{local}".encode()).hexdigest()[:32]
                    result = self.db.execute(
                        "INSERT OR IGNORE INTO inbox(event_id,account,username,source,local_id,title,payload,priority,ts) "
                        "VALUES(?,?,?,?,?,?,?,?,?)", (event, account, username, source, local, title,
                                                      payload, priority(message) if callable(priority) else priority,
                                                      message.get("timestamp", 0)))
                    inserted += result.rowcount
            latest = max([cursor] + [int(m["local_id"]) for m in messages])
            self.db.execute("INSERT INTO inbox_cursor VALUES(?,?,?,?,?) ON CONFLICT(account,username,source) "
                            "DO UPDATE SET local_id=MAX(local_id,excluded.local_id)",
                            (account, username, source, latest, minimum_timestamp))
            return inserted

    def count(self, account=None, min_priority=0):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM inbox WHERE state IN ('pending','processing') "
                                   "AND (? IS NULL OR account=?) AND priority>=?", (account, account, min_priority)).fetchone()[0]

    def claim(self, busy, now=None, limit=6, account=None):
        now = time.time() if now is None else now
        with self.lock, self.db:
            # The head gates each chat; a later arrival cannot jump over its retrying predecessor.
            heads = self.db.execute("""SELECT * FROM inbox i WHERE state='pending' AND (? IS NULL OR i.account=?) AND NOT EXISTS
                (SELECT 1 FROM inbox p WHERE p.account=i.account AND p.username=i.username
                 AND p.state IN ('pending','processing') AND (p.ts<i.ts OR (p.ts=i.ts AND p.rowid<i.rowid)))
                ORDER BY priority DESC, ts DESC""", (account, account)).fetchall()
            head = next((r for r in heads if r["username"] not in busy and r["due"] <= now), None)
            if head is None:
                return None
            rows = self.db.execute("SELECT rowid,* FROM inbox WHERE account=? AND username=? AND state='pending' "
                                   "ORDER BY ts,rowid LIMIT ?", (head["account"], head["username"], limit)).fetchall()
            batch = []
            for row in rows:
                if row["due"] > now:
                    break
                item = dict(row)
                item["message"] = json.loads(item.pop("payload"))
                batch.append(item)
            batch_id = head["batch_id"] or hashlib.sha256("\0".join(r["event_id"] for r in batch).encode()).hexdigest()[:16]
            if head['username'].endswith('@chatroom') and not head['batch_id']:
                speaker=batch[0]['message'].get('sender')
                stop=next((index for index,item in enumerate(batch) if item['message'].get('sender')!=speaker),len(batch))
                batch=batch[:stop]
                batch_id=hashlib.sha256('\0'.join(r['event_id'] for r in batch).encode()).hexdigest()[:16]
            # A saved draft must keep its original batch boundary on retry.
            if head["batch_id"]:
                batch = [r for r in batch if r["batch_id"] == head["batch_id"]]
            for item in batch:
                item["batch_id"] = batch_id
                self.db.execute("UPDATE inbox SET state='processing', batch_id=? WHERE event_id=?",
                                (batch_id, item["event_id"]))
            return batch

    def save_reply(self, batch, reply):
        with self.lock, self.db:
            self.db.executemany("UPDATE inbox SET reply=? WHERE event_id=?", [(reply, r["event_id"]) for r in batch])

    def complete(self, batch, result, now=None, wait=0):
        now = time.time() if now is None else now
        with self.lock, self.db:
            for row in batch:
                attempts = row["attempts"] + (result == "retry")
                state = "pending" if result in ("retry", "defer") else result
                if attempts >= 12:
                    state = "failed"
                delay = wait if result == "defer" else min(300, 3 * 2 ** min(attempts, 7))
                self.db.execute("UPDATE inbox SET state=?,attempts=?,due=? WHERE event_id=?",
                                (state, attempts, now + delay, row["event_id"]))

    def recover(self, terminal_batches, account=None):
        with self.lock, self.db:
            for batch_id in terminal_batches:
                self.db.execute("UPDATE inbox SET state='unconfirmed' WHERE batch_id=? AND state IN ('pending','processing') AND (? IS NULL OR account=?)",
                                (batch_id, account, account))
            return self.db.execute("UPDATE inbox SET state='pending' WHERE state='processing' AND (? IS NULL OR account=?)", (account, account)).rowcount

    def memory_key(self, account, username, legacy=None):
        with self.lock, self.db:
            key = legacy or hashlib.sha256(f"{account}\0{username}".encode()).hexdigest()[:32]
            self.db.execute("INSERT OR IGNORE INTO conversation_alias VALUES(?,?,?)", (account, username, key))
            return self.db.execute("SELECT memory_key FROM conversation_alias WHERE account=? AND username=?",
                                   (account, username)).fetchone()[0]

    def close(self):
        self.db.close()

    def purge(self, retention_days):
        with self.lock, self.db:
            return self.db.execute("DELETE FROM inbox WHERE state NOT IN ('pending','processing') AND ts<?",
                                   (time.time() - retention_days * 86400,)).rowcount


class Reader(threading.Thread):
    def __init__(self, factory, inbox, cfg, mention, on_messages=None):
        super().__init__(name="wechat-db-reader", daemon=True)
        self.factory, self.inbox, self.cfg, self.mention = factory, inbox, cfg, mention
        self.stop_event = threading.Event()
        self.last_ok = 0
        self.errors = 0
        self._signatures = {}
        self.account = None
        self.on_messages = on_messages
        self._memory_seeded = False

    def poll(self, reader):
        import re
        if self.account is not None and self.account != reader.account:
            raise RuntimeError("reader_account_changed")
        self.account = reader.account
        with self.inbox.lock:
            bootstrap = self.inbox.db.execute("SELECT started FROM inbox_bootstrap WHERE account=?", (reader.account,)).fetchone()
        epoch = bootstrap[0] if bootstrap else int(time.time())
        sources = sorted(k for k in reader.keys if re.fullmatch(r"message/message_\d+\.db", k))
        sessions = reader.sessions(limit=10000)
        if self.on_messages and not self._memory_seeded:
            cutoff=time.time()-self.cfg['memory']['retention_days']*86400
            for scene in sessions:
                if scene['title'] in self.cfg['policy']['skip_contacts']:
                    continue
                for source in sources:
                    history=reader.messages(scene['username'],limit=200,source=source)
                    recent=[m for m in history if m.get('timestamp',0)>=cutoff]
                    if recent:
                        self.on_messages(reader,scene,recent,source)
            self._memory_seeded=True
        count = 0
        for session in sessions:
            user, title = session["username"], session["title"]
            if title in self.cfg["policy"]["skip_contacts"]:
                continue
            for source in sources:
                cursor = self.inbox.cursor(reader.account, user, source)
                signature = (session["last_timestamp"], session["unread"])
                # Periodic reconciliation catches same-second arrivals and delayed SessionTable updates.
                cached = self._signatures.get((user, source))
                if cached and cached[0] == signature and time.monotonic() - cached[1] < 15:
                    continue
                baseline = None
                minimum_timestamp = 0
                if cursor is not None:
                    with self.inbox.lock:
                        minimum_timestamp = self.inbox.db.execute("SELECT minimum_timestamp FROM inbox_cursor "
                            "WHERE account=? AND username=? AND source=?", (reader.account, user, source)).fetchone()[0]
                if cursor is None and bootstrap:
                    baseline = cursor = 0
                    minimum_timestamp = epoch
                elif cursor is None:
                    history = reader.messages(user, limit=max(30, int(session["unread"]) + 1), source=source)
                    eligible = [m for m in history if not m["outgoing"] and m["timestamp"] >= time.time() - 172800]
                    unread = min(int(session["unread"]), len(eligible))
                    baseline = eligible[-unread]["local_id"] - 1 if unread else max((m["local_id"] for m in history), default=0)
                    cursor = baseline
                priority = lambda m: (4 if not user.endswith("@chatroom") else
                                      3 if self.mention(m.get("text", "")) or m.get('quote_sender')==reader.account else 1)
                # A bounded page is revisited next poll instead of monopolising ingestion.
                messages = reader.messages(user, limit=200, after_id=cursor, source=source)
                if self.on_messages and messages:
                    self.on_messages(reader,session,messages,source)
                count += self.inbox.ingest(reader.account, user, source, title, messages, priority, baseline, minimum_timestamp)
                if len(messages) < 200:
                    self._signatures[user, source] = (signature, time.monotonic())
        with self.inbox.lock, self.inbox.db:
            self.inbox.db.execute("INSERT OR IGNORE INTO inbox_bootstrap VALUES(?,?)", (reader.account, epoch))
        self.last_ok = time.monotonic()
        return count

    def run(self):
        reader = None
        while not self.stop_event.is_set():
            try:
                reader = reader or self.factory()
                count = self.poll(reader)
                if count:
                    LOG.info("db_inbox_ingested count=%d pending=%d", count, self.inbox.count())
                self.errors = 0
            except Exception as exc:
                self.errors += 1
                reader = None
                if self.errors == 1 or self.errors % 30 == 0:
                    LOG.warning("db_ingestion_failed=%s", type(exc).__name__)
            self.stop_event.wait(1.0 if not self.errors else 3.0)


def run(cfg, config_path=None):
    from database_service import run as run_service
    return run_service(cfg, config_path)
