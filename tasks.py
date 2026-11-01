"""Persistent, scene-scoped self reminders; delivery stays with the GUI executor."""
import hashlib
import math
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from conversation_memory import own_text


CN = timezone(timedelta(hours=8), 'Asia/Shanghai')
LABELS = dict(pending='待办', sending='发送中', confirmed='已提醒', failed='提醒失败',
              unconfirmed='发送未确认，不会自动重发', cancelled='已取消', completed='已完成',
              expired='已过期，超过24小时未提醒')
ACTIVE = ('pending', 'sending', 'unconfirmed')
SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    account TEXT NOT NULL,
    person_id TEXT NOT NULL,
    username TEXT NOT NULL,
    title TEXT NOT NULL,
    session_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    due_at REAL,
    created_at REAL NOT NULL,
    event_id TEXT NOT NULL,
    state TEXT NOT NULL,
    UNIQUE(account,event_id)
);
CREATE INDEX IF NOT EXISTS tasks_due ON tasks(account,state,due_at);
CREATE INDEX IF NOT EXISTS tasks_owner ON tasks(account,person_id,username);
"""


def _number(text):
    if text.isdecimal():
        return int(text)
    digits = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
              '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
    if text in digits:
        return digits[text]
    if '十' in text and text.count('十') == 1:
        left, right = text.split('十')
        if (not left or left in digits) and (not right or right in digits):
            return digits.get(left, 1) * 10 + digits.get(right, 0)
    raise ValueError('请用明确的数字描述时间。')


def _due(text, stamp):
    """Parse explicit dates or relative durations, without guessing calendar periods."""
    number = r'[0-9零一二两三四五六七八九十]+'
    relative = re.fullmatch(r'(' + number + r'|半)(秒|分钟|小时|天)后', text)
    if relative:
        value = 0.5 if relative[1] == '半' else _number(relative[1])
        due = stamp + value * {'秒': 1, '分钟': 60, '小时': 3600, '天': 86400}[relative[2]]
    else:
        date = re.fullmatch(r'(?:(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?|(今天|明天|后天))\s*'
                            r'(凌晨|早上|上午|中午|下午|晚上)?\s*(' + number + r')'
                            r'(?::(\d{2})|点(?:(半)|(' + number + r')分?)?)', text)
        if not date:
            raise ValueError('请说明具体日期和时间，例如“明天上午9点提醒我开会”，或“10分钟后提醒我喝水”。')
        now = datetime.fromtimestamp(stamp, CN)
        if date[4]:
            day = now.date() + timedelta(days={'今天': 0, '明天': 1, '后天': 2}[date[4]])
        else:
            day = datetime(int(date[1]), int(date[2]), int(date[3])).date()
        period, hour = date[5], _number(date[6])
        minute = int(date[7]) if date[7] else 30 if date[8] else _number(date[9]) if date[9] else 0
        periods = {'凌晨': range(0, 6), '早上': range(5, 12), '上午': range(6, 12),
                   '中午': (11, 12, 1), '下午': range(1, 13), '晚上': range(5, 12)}
        if period and hour not in periods[period]:
            raise ValueError('时段与小时不一致或午夜日期不明确，请使用24小时制。')
        if period in ('下午', '晚上', '中午'):
            if hour < 12 and not (period == '中午' and hour == 11):
                hour += 12
        elif not period and '点' in text and 1 <= hour <= 12:
            raise ValueError('请说明上午或下午，或用24小时制，例如“明天09:00提醒我开会”。')
        due = datetime(day.year, day.month, day.day, hour, minute, tzinfo=CN).timestamp()
    if due <= stamp:
        raise ValueError('这个时间已经过去，请给一个未来时间。')
    if due - stamp > 365 * 86400:
        raise ValueError('最多支持365天以内的提醒。')
    return due


def _command_text(job):
    focus = job.get('focus', {}).get('message')
    if (not isinstance(focus, dict) or focus.get('outgoing') or focus.get('quote_sender') or
            (focus.get('type', 1) & 0xffff) != 1):
        return ''
    if focus.get('sender') != job.get('person_id'):
        return ''
    text = own_text(focus)
    if job.get('is_mention'):
        names = set(job.get('self_names') or [])
        for name in sorted(names, key=len, reverse=True):
            if name:
                text = re.sub(r'^@' + re.escape(name) + r'[\s\u2005]+', '', text, count=1)
    return re.sub(r'^(?:请|帮我)\s*', '', text).strip()


def is_composite_request(job):
    """Let the model split a compound request; simple commands keep the direct path."""
    text = _command_text(job)
    return bool(re.search(r'提醒我|(?:添加|新增|创建)待办', text) and
                re.search(r'然后|并且|同时|顺便|(?:先|再).*(?:查|搜|分析|提醒)|[，,；;\n].*(?:提醒我|待办)', text))


class Tasks:
    def __init__(self, store):
        self.store = store
        with store._lock, store._conn:
            store._conn.executescript(SCHEMA)

    def create_from_tool(self, job, request, now=None):
        """Only exact native request spans may reach the existing persistent writer."""
        original = _command_text(job)
        native = job.get('focus', {}).get('message') or {}
        if (not isinstance(request, str) or not 1 <= len(request) <= 500 or not original
                or request not in own_text(native) or request != request.strip()
                or re.search(r'[“”「」『』\"\'`]|(?:他|她|别人|有人)(?:说|让|要求)|引用|转发', original)):
            return '任务请求必须来自你本轮原文的连续本人指令，未创建任务。'
        boundary = (r'(?:^|[，,；;。！？!?\n])\s*'
                    r'(?:(?:然后|再|同时|并且|顺便|另外|最后|以及|并|请|帮我)\s*)*'
                    + re.escape(request) + r'(?=$|[，,；;。！？!?\n])')
        if not re.search(boundary, original):
            return '任务片段须包含完整独立指令，不能省略否定、说话人或任务内容，未创建任务。'
        mentions = re.findall(r'@([^\s@，,：:？?。！!]+)', own_text(native))
        if mentions and (not job.get('is_mention') or any(name not in (job.get('self_names') or []) for name in mentions)):
            return '不能根据指向其他人的消息创建任务。'
        if not re.match(r'^(?:(?:请|帮我)\s*)?(?:(?:添加|新增|创建)待办|[^，,。；;\n]{1,80}提醒我)', request):
            return '只支持创建本人提醒或待办，未创建任务。'
        source_id = str(job.get('focus', {}).get('event_id') or job.get('event_id') or '')
        if not source_id:
            return '缺少可靠消息标识，未创建任务。'
        derived = dict(job)
        derived_native = dict(native, text=request)
        event_id = source_id if request.rstrip('。！!') == original.rstrip('。！!') else (
            source_id + ':tool:' + hashlib.sha256(request.encode('utf-8')).hexdigest())
        derived['focus'] = dict(job['focus'], event_id=event_id, message=derived_native)
        derived['event_id'] = event_id
        receipt = self.handle(derived, now=now)
        return receipt or '任务片段不是可识别的完整创建指令，未创建任务。'

    def handle(self, job, now=None):
        """Return a persisted command result, or None for ordinary inbound text."""
        text = _command_text(job).rstrip('。！!')
        query = text in ('查看待办', '查看任务', '我的待办', '我的任务')
        change = re.fullmatch(r'(取消|完成)(?:任务|待办)\s*([a-fA-F0-9]{10})', text)
        invalid_change = re.match(r'(?:取消|完成)(?:任务|待办)', text)
        todo = re.fullmatch(r'(?:添加|新增|创建)待办\s*[：:]?\s*(.*)', text)
        reminder = re.fullmatch(r'(.*?)提醒(我|[^\s，。]{1,12})(.*)', text)
        if reminder and reminder[1].strip() and not re.fullmatch(
                r'(?:[0-9零一二两三四五六七八九十年月日天点半分秒钟小时个后今明早上午中下晚凌晨周星期本末过会儿：:\-\s])+',
                reminder[1]):
            reminder = None
        if not (query or change or invalid_change or todo or reminder):
            return None
        job['task_command'] = True
        scope = tuple(str(job.get(key) or '') for key in ('account', 'person_id', 'username'))
        if not all(scope) or not job.get('session_key') or job.get('kind') not in ('private', 'group'):
            return '无法确认创建者和当前会话，未操作任务。'
        stamp = time.time() if now is None else now
        if invalid_change and not change:
            return '请提供完整任务ID，例如“取消任务 abc123def0”；可先发送“查看待办”。'
        if query:
            self._expire(scope[0], stamp)
            rows = self.store._rows('SELECT * FROM tasks WHERE account=? AND person_id=? AND username=? '
                                    "ORDER BY CASE WHEN state IN ('pending','sending','unconfirmed') THEN 0 ELSE 1 END,"
                                    'created_at DESC,id LIMIT 40', scope)
            if not rows:
                return '当前会话没有你的任务。'
            total = self.store._rows('SELECT COUNT(*) AS n FROM tasks WHERE account=? AND person_id=? AND username=?', scope)[0]['n']
            active = sum(row['state'] in ACTIVE for row in rows)
            header = f'当前会话共{total}项，活跃{active}项。北京时间；内容仅前12字，…表示省略；未确认不自动重发。'
            lines = []
            for row in rows:
                when = datetime.fromtimestamp(row['due_at'], CN).strftime('%Y-%m-%d %H:%M') if row['due_at'] is not None else '无定时'
                state = '未确认' if row['state'] == 'unconfirmed' else LABELS[row['state']]
                body = ' '.join(row['body'].split())
                excerpt = body[:12] + ('…' if len(body) > 12 else '')
                line = f"{row['id']} [{state}] {when} {excerpt}"
                # Reserve the footer before admitting optional history; all 20 active rows fit.
                if len(header) + sum(len(item)+1 for item in lines) + len(line) + 40 > 1100:
                    break
                lines.append(line)
            footer = f'\n显示{len(lines)}/{total}项。' + ('其余历史记录已省略。' if len(lines) < total else '')
            return header + '\n' + '\n'.join(lines) + footer
        if change:
            task_id = change[2].lower()
            with self.store._lock, self.store._conn:
                row = self.store._conn.execute('SELECT * FROM tasks WHERE id=? AND account=? AND person_id=? AND username=?',
                                               (task_id,) + scope).fetchone()
                if not row:
                    return '当前会话未找到你的这个任务。'
                if row['state'] == 'sending':
                    return '任务正在发送中，暂时不能更改；请稍后查看任务状态。'
                state = 'cancelled' if change[1] == '取消' else 'completed'
                if row['state'] not in ('cancelled', 'completed'):
                    self.store._conn.execute('UPDATE tasks SET state=? WHERE id=?', (state, task_id))
                else:
                    state = row['state']
            return f'任务 {task_id} {LABELS[state]}。'
        event_id = str(job.get('focus', {}).get('event_id') or job.get('event_id') or '')
        if not event_id:
            return '缺少可靠消息标识，未创建任务。'
        previous = self.store._rows('SELECT * FROM tasks WHERE account=? AND event_id=?', (scope[0], event_id))
        if previous:
            row = previous[0]
            if (row['person_id'], row['username']) != scope[1:]:
                return '无法验证这条任务请求，未操作任务。'
            return self._created(row)
        try:
            due = None
            if todo:
                body = todo[1].strip()
            else:
                if reminder[2] != '我':
                    return '只支持提醒你本人，并发送到创建任务的当前会话。'
                source_stamp = job['focus']['message'].get('timestamp', stamp)
                if (isinstance(source_stamp, bool) or not isinstance(source_stamp, (int, float)) or
                        not math.isfinite(source_stamp) or source_stamp <= 0 or source_stamp > stamp + 60):
                    return '原消息时间无效或明显晚于当前时间，未创建提醒；请重新说明未来的提醒时间。'
                due = _due(reminder[1].strip(), source_stamp)
                if due <= stamp:
                    return '按原消息时间计算，提醒时间已过去，未创建提醒；请重新给出未来时间。'
                body = reminder[3].lstrip('，,:： ').strip()
            if not 1 <= len(body) <= 300:
                return '请提供1到300字的提醒内容。'
            if re.search(r'@|提醒(?:他|她|他们|她们)|(?:发(?:送)?(?:消息|信息|微信)|转告|通知)(?:给|到)|'
                         r'(?<![a-z])(?:shell|bash|powershell|cmd|sudo|ssh|curl|wget)(?![a-z])|'
                         r'执行(?:命令|脚本)|运行(?:命令|脚本)', body, re.I):
                return '任务仅支持当前会话的本人提醒或待办，不执行命令或替他人安排发送。'
        except (ValueError, OverflowError):
            return '时间无效或不明确。请给出365天内的未来时间，例如“明天上午9点提醒我开会”。'
        row = dict(id=secrets.token_hex(5), account=scope[0], person_id=scope[1], username=scope[2],
                   title=str(job.get('title') or ''), session_key=job['session_key'], kind=job['kind'],
                   body=body, due_at=due, created_at=stamp, event_id=event_id, state='pending')
        with self.store._lock, self.store._conn:
            # One INSERT arbitrates both event redelivery and the per-owner active cap across connections.
            keys = tuple(row)
            self.store._conn.execute('INSERT OR IGNORE INTO tasks (' + ','.join(keys) + ') SELECT ' +
                                     ','.join('?' for _ in keys) + ' WHERE '
                                     '(SELECT COUNT(*) FROM tasks WHERE account=? AND person_id=? '
                                     "AND state IN ('pending','sending','unconfirmed'))<20",
                                     tuple(row.values()) + scope[:2])
            saved = self.store._conn.execute('SELECT * FROM tasks WHERE account=? AND event_id=?',
                                            (scope[0], row['event_id'])).fetchone()
            result = dict(saved) if saved else None
        if result and (result['person_id'], result['username']) != scope[1:]:
            return '无法验证这条任务请求，未操作任务。'
        return self._created(result) if result else '你的活跃任务已达20个，请先完成或取消一些任务。'

    @staticmethod
    def _describe(row):
        when = datetime.fromtimestamp(row['due_at'], CN).strftime('%Y-%m-%d %H:%M') + '（北京时间）' if row['due_at'] is not None else '无定时'
        return f"{row['id']} [{LABELS[row['state']]}] {when} {row['body']}"

    def _created(self, row):
        suffix = '；仅在当前会话提醒。' if row['due_at'] is not None else '；可在当前会话查看或完成。'
        return '已创建任务：' + self._describe(row) + suffix

    def _expire(self, account, stamp):
        self.store._write("UPDATE tasks SET state='expired' WHERE account=? AND state='pending' AND due_at<?",
                          (account, stamp - 86400))

    def due(self, account, now=None):
        stamp = time.time() if now is None else now
        self._expire(account, stamp)
        return self.store._rows("SELECT * FROM tasks WHERE account=? AND state='pending' AND due_at<=? "
                                'AND due_at>=? ORDER BY due_at,id LIMIT 20', (account, stamp, stamp - 86400))

    def claim(self, task_id):
        return bool(self.store._write("UPDATE tasks SET state='sending' WHERE id=? AND state='pending' AND due_at IS NOT NULL",
                                      (task_id,)).rowcount)

    def finish(self, task_id, state):
        if state not in ('pending', 'confirmed', 'failed', 'unconfirmed'):
            raise ValueError('Invalid task delivery state')
        self.store._write("UPDATE tasks SET state=? WHERE id=? AND state='sending'", (state, task_id))

    def recover(self):
        return self.store._write("UPDATE tasks SET state='unconfirmed' WHERE state='sending'").rowcount
