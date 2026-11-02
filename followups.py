"""Private important-event followups, scheduled only after explicit native consent."""
import math
import hashlib
import re
import secrets
import time
from datetime import datetime

from conversation_memory import own_text
from tasks import ACTIVE, CN, Tasks, _due


SCHEMA = """
CREATE TABLE IF NOT EXISTS followup_proposals (
    account TEXT NOT NULL, event_id TEXT NOT NULL,
    person_id TEXT NOT NULL, username TEXT NOT NULL, session_key TEXT NOT NULL,
    topic TEXT NOT NULL, due_at REAL NOT NULL, created_at REAL NOT NULL,
    task_id TEXT, PRIMARY KEY(account,event_id)
);
CREATE INDEX IF NOT EXISTS followup_scope ON followup_proposals
    (account,person_id,username,session_key,created_at);
"""
NUMBER = r'[0-9零一二两三四五六七八九十]+'
DATE_TIME = (r'(?:(?:\d{4}[-年]\d{1,2}[-月]\d{1,2}日?)|今天|明天|后天)\s*'
             r'(?:凌晨|早上|上午|中午|下午|晚上)?\s*' + NUMBER +
             r'(?::\d{2}|点(?:半|' + NUMBER + r'分?)?)')
SPECIFIC_TIME = '请给出具体的未来日期和时间，例如“明天下午3点面试结束后问问我进展”。'


def _native(job, stamp):
    focus = job.get('focus')
    if not isinstance(focus, dict):
        return None
    message = focus.get('message')
    scope = tuple(job.get(key) for key in ('account', 'person_id', 'username', 'session_key'))
    if (not all(isinstance(value, str) and value for value in scope)
            or job.get('kind') != 'private' or scope[1] != scope[2]
            or '@chatroom' in scope[2] or not isinstance(message, dict)
            or message.get('outgoing') is not False or message.get('type') != 1
            or message.get('sender') != scope[1]
            or message.get('quote_sender') or message.get('quote_text')
            or not isinstance(focus.get('source'), str) or not focus['source']
            or not isinstance(message.get('local_id'), int)
            or isinstance(message['local_id'], bool) or message['local_id'] <= 0
            or not isinstance(focus.get('event_id'), str) or not focus['event_id']):
        return None
    sent = message.get('timestamp')
    expected=hashlib.sha256((scope[0]+'\0'+scope[2]+'\0'+focus['source']+'\0'+str(message['local_id'])).encode()).hexdigest()[:32]
    if focus['event_id']!=expected:return None
    if (isinstance(sent, bool) or not isinstance(sent, (float, int))
            or not math.isfinite(sent) or sent <= 0 or sent > stamp+60):
        return None
    text = own_text(message).strip().rstrip('。！!')
    # shortcut: only simple firsthand statements qualify; expand with reviewed examples.
    if (not text or len(text) > 300 or re.search(
            r'[“”「」『』\"\'`@\n？?]|转发|引用|如果|假如|可能|不要|别|不需要|'
            r'(?:他|她|别人|有人)(?:说|让|要求)|(?<!我)的(?:面试|考试|答辩|项目|汇报|比赛)|系统|指令|忽略|执行|脚本|发送|发消息', text)):
        return None
    return text, scope, sent, focus['event_id']


def _event(text, sent):
    match = re.fullmatch(r'(?:我)?(' + DATE_TIME + r')\s*(?:有|要参加|参加|要去|去|要)?\s*([^，,；;。！？!?\n]{1,80})', text)
    if not match:
        return None
    topic = match[2].strip()
    if (not re.fullmatch(r'(?:面试|考试|答辩|汇报|比赛|项目(?:汇报|验收|答辩)?)', topic)
            or re.search(r'不|取消|推迟|改期|问问|提醒|吗|是否|\d|[今明后]天', topic)):
        return None
    return topic, _due(match[1], sent)


class Followups:
    def __init__(self, store):
        self.store = store
        self.tasks = Tasks(store)
        with store._lock, store._conn:
            store._conn.executescript(SCHEMA)

    def observe(self, job, now=None):
        """Persist a ten-minute proposal; returned offer is optional reply context."""
        stamp = time.time() if now is None else now
        native = _native(job, stamp)
        if not native:
            return None
        text, scope, sent, event_id = native
        if stamp-sent > 600:
            return None
        try:
            event = _event(text, sent)
        except (ValueError, OverflowError):
            return None
        if not event or event[1] <= stamp:
            return None
        topic, due = event
        with self.store._lock, self.store._conn:
            self.store._conn.execute(
                'INSERT OR IGNORE INTO followup_proposals '
                '(account,event_id,person_id,username,session_key,topic,due_at,created_at) '
                'VALUES (?,?,?,?,?,?,?,?)',
                (scope[0], event_id, *scope[1:], topic, due, sent))
            proposal = self.store._conn.execute(
                'SELECT * FROM followup_proposals WHERE account=? AND event_id=?',
                (scope[0], event_id)).fetchone()
        if not proposal or tuple(proposal[key] for key in ('person_id', 'username', 'session_key')) != scope[1:] or proposal['task_id']:
            return None
        when = datetime.fromtimestamp(proposal['due_at']+7200, CN).strftime('%Y-%m-%d %H:%M')
        return f'要不要在{when}（北京时间）问问你{proposal["topic"]}的进展？同意可说“好，之后问问我”；尚未安排。'

    def handle(self, job, now=None):
        """Handle one-step requests or explicit consent, returning persisted receipts."""
        stamp = time.time() if now is None else now
        native = _native(job, stamp)
        if not native:
            return None
        text, scope, sent, event_id = native
        if text in ('查看跟进','我的跟进'):
            rows=self.store._rows("SELECT * FROM tasks WHERE account=? AND person_id=? AND username=? AND session_key=? AND event_id LIKE 'followup:%' ORDER BY created_at DESC LIMIT 20",scope)
            return '当前私聊的跟进事项：\n'+'\n'.join(self.tasks._describe(row) for row in rows) if rows else '当前私聊没有你的跟进事项。'
        cancel=re.fullmatch(r'取消跟进\s*([a-fA-F0-9]{10})',text)
        if cancel:
            with self.store._lock,self.store._conn:
                row=self.store._conn.execute("SELECT * FROM tasks WHERE id=? AND account=? AND person_id=? AND username=? AND session_key=? AND event_id LIKE 'followup:%'",(cancel[1].lower(),)+scope).fetchone()
                if not row:return '当前私聊未找到你的这个跟进事项。'
                if row['state'] in ('sending','unconfirmed'):return '该跟进正在发送或送达未确认，保留原记录，不会自动重发。'
                if row['state']!='pending':return '该跟进状态为'+self.tasks._describe(row)+'，未更改。'
                self.store._conn.execute("UPDATE tasks SET state='cancelled' WHERE id=? AND state='pending'",(row['id'],))
            return '已取消跟进：'+row['id']
        consent = re.fullmatch(r'(?:好|好的|可以|行|同意)[，,\s]*(.+?)问问我(?:进展|结果|情况)?', text)
        direct = re.fullmatch(r'(.+?)(?:结束后|之后|以后)问问我(?:进展|结果|情况)?', text)
        if not consent and not direct:
            return None
        job['task_command'] = True
        task_event = 'followup:' + event_id
        existing = self.store._rows('SELECT * FROM tasks WHERE account=? AND event_id=?', (scope[0], task_event))
        if existing:
            row = existing[0]
            return self.tasks._created(row) if tuple(row[key] for key in ('person_id', 'username', 'session_key')) == scope[1:] else '无法验证这条跟进请求，未创建任务。'
        proposal = None
        try:
            if consent:
                rows = self.store._rows(
                    'SELECT * FROM followup_proposals WHERE account=? AND person_id=? '
                    'AND username=? AND session_key=? ORDER BY created_at DESC,rowid DESC LIMIT 1', scope)
                proposal = rows[0] if rows else None
                if (not proposal or stamp-proposal['created_at'] > 600
                        or sent < proposal['created_at'] or stamp-sent > 600):
                    return '没有当前会话中十分钟内待同意的跟进提议。' + SPECIFIC_TIME
                if proposal['task_id']:
                    saved = self.store._rows('SELECT * FROM tasks WHERE id=?', (proposal['task_id'],))
                    return self.tasks._created(saved[0]) if saved else '跟进记录无效，未创建任务。'
                topic = proposal['topic']
                due = proposal['due_at']+7200 if consent[1] in ('之后', '结束后', '以后') else _due(consent[1], sent)
            else:
                event = _event(direct[1], sent)
                if not event:
                    return SPECIFIC_TIME
                topic, event_due = event
                due = event_due+7200
            if due <= stamp or due-stamp > 365*86400:
                return SPECIFIC_TIME
        except (ValueError, OverflowError):
            return SPECIFIC_TIME
        row = dict(id=secrets.token_hex(5), account=scope[0], person_id=scope[1],
                   username=scope[2], title=str(job.get('title') or ''), session_key=scope[3],
                   kind='private', body=f'之前提到的{topic}进展怎么样？', due_at=due,
                   created_at=stamp, event_id=task_event, state='pending')
        with self.store._lock, self.store._conn:
            # The proposal check and cap live in the INSERT so concurrent consent creates once.
            keys = tuple(row)
            guard = (' AND EXISTS (SELECT 1 FROM followup_proposals WHERE account=? AND event_id=? AND task_id IS NULL)'
                     if proposal else '')
            params = tuple(row.values()) + scope[:2] + ((scope[0], proposal['event_id']) if proposal else ())
            self.store._conn.execute(
                'INSERT OR IGNORE INTO tasks (' + ','.join(keys) + ') SELECT ' +
                ','.join('?' for _ in keys) + ' WHERE (SELECT COUNT(*) FROM tasks '
                "WHERE account=? AND person_id=? AND state IN ('pending','sending','unconfirmed'))<20" + guard,
                params)
            saved = self.store._conn.execute('SELECT * FROM tasks WHERE account=? AND event_id=?',
                                            (scope[0], task_event)).fetchone()
            if saved:
                if proposal:
                    self.store._conn.execute('UPDATE followup_proposals SET task_id=? WHERE account=? AND event_id=? AND task_id IS NULL',
                                             (saved['id'], scope[0], proposal['event_id']))
                else:
                    self.store._conn.execute(
                        'INSERT OR IGNORE INTO followup_proposals '
                        '(account,event_id,person_id,username,session_key,topic,due_at,created_at,task_id) '
                        'VALUES (?,?,?,?,?,?,?,?,?)',
                        (scope[0], event_id, *scope[1:], topic, due-7200, sent, saved['id']))
                result = dict(saved)
            else:
                result = None
        return self.tasks._created(result) if result else '未创建跟进任务；活跃任务最多20个，或该提议已经安排。'

    def purge(self,now=None):
        stamp=time.time() if now is None else now
        self.store._write('DELETE FROM followup_proposals WHERE task_id IS NULL AND created_at<?',(stamp-600,))

    def delivery_text(self, task):
        """Return the exact question for a persisted followup; None for ordinary tasks."""
        rows = self.store._rows('SELECT 1 FROM followup_proposals WHERE task_id=? AND account=? '
                                'AND person_id=? AND username=? AND session_key=?',
                                tuple(task[key] for key in ('id', 'account', 'person_id', 'username', 'session_key')))
        return task['body'] if rows else None
