"""Owner-authorized native text commands; fixed boolean overrides, never config-file writes."""
import copy
import math
import time
from datetime import datetime, timedelta, timezone


SCOPES = {
    'group': {'policy.group_strict_mention': True, 'policy.group_interject_enabled': False,
              'proactive.group_interjections': False},
    'moments': {'moments.interaction_enabled': False, 'moments.like_enabled': False,
                'moments.comment_enabled': False},
    'collect': {'stickers.collect_enabled': False, 'stickers.passive_collect': False},
    'auto_send': {'stickers.auto_send': False},
}
COMMANDS = {
    '今晚群里只回@': ('group', 'tonight'),
    '群里只回@': ('group', 'pause'),
    '群聊只回@': ('group', 'pause'),
    '恢复群聊接话': ('group', 'restore'),
    '恢复群聊主动接话': ('group', 'restore'),
    '暂停朋友圈互动': ('moments', 'pause'),
    '恢复朋友圈互动': ('moments', 'restore'),
    '暂停自动收藏': ('collect', 'pause'),
    '恢复自动收藏': ('collect', 'restore'),
    '暂停自动发表情': ('auto_send', 'pause'),
    '恢复自动发表情': ('auto_send', 'restore'),
    '查看小i设置': ('status', 'status'),
    '管理状态': ('status', 'status'),
}
LABELS = {'group': '群聊', 'moments': '朋友圈互动', 'collect': '自动收藏', 'auto_send': '自动发表情'}
KEYS = frozenset(key for values in SCOPES.values() for key in values)
SHANGHAI = timezone(timedelta(hours=8))


def is_command(text):
    return isinstance(text, str) and text.strip() in COMMANDS


def _finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 512 and bool(value.strip()) and '\0' not in value


class RuntimeControls:
    def __init__(self, store, account):
        if not _identifier(account):
            raise ValueError('A native account identifier is required')
        self.store, self.account = store, account
        with store._lock, store._conn:
            store._conn.executescript('''
                CREATE TABLE IF NOT EXISTS runtime_overrides(
                    account TEXT NOT NULL,key TEXT NOT NULL,value INTEGER,expires REAL,
                    PRIMARY KEY(account,key));
                CREATE TABLE IF NOT EXISTS runtime_control_state(
                    account TEXT NOT NULL,scope TEXT NOT NULL,source TEXT NOT NULL,
                    event TEXT NOT NULL,ts REAL NOT NULL,local_id INTEGER NOT NULL,
                    count INTEGER NOT NULL,ack TEXT NOT NULL,PRIMARY KEY(account,scope));
            ''')

    def _native(self, job):
        if not isinstance(job, dict) or job.get('account') != self.account or job.get('kind') != 'private':
            return None
        person, scene = job.get('person_id'), job.get('username')
        focus = job.get('focus')
        if (not _identifier(person) or person != scene or person == self.account or person.isdecimal()
                or person.endswith('@chatroom') or not isinstance(focus, dict)):
            return None
        message = focus.get('message')
        if not isinstance(message, dict):
            return None
        stamp, local = message.get('timestamp'), message.get('local_id')
        if (message.get('sender') != person or message.get('outgoing') is not False
                or type(message.get('type')) is not int or message['type'] != 1
                or not isinstance(message.get('text'), str)
                or not _finite_number(stamp) or stamp <= 0 or type(local) is not int or local <= 0
                or not _identifier(focus.get('event_id')) or not _identifier(focus.get('source'))
                or any(focus.get(key, value) != value for key, value in
                       (('account', self.account), ('username', scene), ('local_id', local)))
                or any(job.get(key) or message.get(key) for key in
                       ('quote_sender', 'quote_text', 'forwarded', 'is_forwarded', 'forward', 'forward_source'))):
            return None
        try:
            datetime.fromtimestamp(stamp, SHANGHAI)
        except (ValueError, OverflowError, OSError):
            return None
        return focus['source'], focus['event_id'], stamp, local, message['text'].strip()

    def _active(self, now):
        active = {}
        for row in self.store._conn.execute('SELECT key,value,expires FROM runtime_overrides WHERE account=?',
                                           (self.account,)):
            value, expires = row['value'], row['expires']
            if row['key'] not in KEYS or type(value) is not int or value not in (0, 1):
                continue
            if expires is not None and (not _finite_number(expires) or expires <= now):
                continue
            active[row['key']] = (bool(value), expires)
        return active

    def apply(self, base_cfg, now=None):
        """Return a private copy; expiry and restore reveal the supplied deployment defaults."""
        now = time.time() if now is None else now
        effective = copy.deepcopy(base_cfg)
        with self.store._lock:
            active = self._active(now)
        for key, (value, _) in active.items():
            section, name = key.split('.')
            if not isinstance(effective.get(section), dict):
                effective[section] = {}
            effective[section][name] = value
        return effective

    def _status(self, now):
        active = self._active(now)
        lines = ['小i管理设置（未覆盖项沿用部署配置）：']
        for scope, keys in SCOPES.items():
            overrides = [active[key] for key in keys if key in active]
            state = ('只回@' if scope == 'group' else '已暂停') if overrides else '部署配置'
            expiry = next((expires for _, expires in overrides if expires is not None), None)
            if expiry is not None:
                state += '，至北京时间' + datetime.fromtimestamp(expiry, SHANGHAI).strftime('%m-%d %H:%M')
            lines.append(LABELS[scope] + '：' + state)
        count = self.store._conn.execute('SELECT COALESCE(SUM(count),0) FROM runtime_control_state WHERE account=?',
                                         (self.account,)).fetchone()[0]
        lines.append('已处理管理指令：' + str(count) + '次。')
        return '\n'.join(lines)

    def handle(self, job, is_owner, now=None):
        """The caller must verify owner binding; display names and message text grant no authority."""
        if is_owner is not True:
            return None
        native = self._native(job)
        if native is None or native[-1] not in COMMANDS:
            return None
        source, event, stamp, local, text = native
        scope, action = COMMANDS[text]
        now = time.time() if now is None else now
        if not _finite_number(now) or now <= 0:
            raise ValueError('A finite current timestamp is required')
        expires = None
        if action == 'tonight':
            expires = (datetime.fromtimestamp(now, SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
                       + timedelta(days=1)).timestamp()
        with self.store._lock, self.store._conn:
            previous = self.store._conn.execute('SELECT * FROM runtime_control_state WHERE account=? AND scope=?',
                                                (self.account, scope)).fetchone()
            if previous:
                if source == previous['source'] and event == previous['event']:
                    return previous['ack']
                # A fixed watermark survives restore; delayed older controls must not reapply.
                if (stamp < previous['ts'] or (source == previous['source'] and local <= previous['local_id'])
                        or (stamp == previous['ts'] and source != previous['source'])):
                    return '这条管理指令已处理或已被更新设置覆盖，当前设置未更改。'
            if action == 'restore':
                self.store._conn.executemany('DELETE FROM runtime_overrides WHERE account=? AND key=?',
                                             [(self.account, key) for key in SCOPES[scope]])
                ack = LABELS[scope] + '：已恢复部署配置。'
            elif action != 'status':
                self.store._conn.executemany('INSERT INTO runtime_overrides(account,key,value,expires) VALUES(?,?,?,?) '
                    'ON CONFLICT(account,key) DO UPDATE SET value=excluded.value,expires=excluded.expires',
                    [(self.account, key, int(value), expires) for key, value in SCOPES[scope].items()])
                ack = ('今晚群里只回@，北京时间零点恢复部署配置。' if action == 'tonight' else
                       '群里只回@，已暂停群聊主动接话。' if scope == 'group' else LABELS[scope] + '已暂停。')
            else:
                ack = ''
            self.store._conn.execute('INSERT INTO runtime_control_state VALUES(?,?,?,?,?,?,?,?) '
                'ON CONFLICT(account,scope) DO UPDATE SET source=excluded.source,event=excluded.event,ts=excluded.ts,'
                'local_id=excluded.local_id,count=runtime_control_state.count+1,ack=excluded.ack',
                (self.account, scope, source, event, stamp, local, 1, ack))
            if action == 'status':
                ack = self._status(now)
                self.store._conn.execute('UPDATE runtime_control_state SET ack=? WHERE account=? AND scope=?',
                                         (ack, self.account, scope))
        return ack
