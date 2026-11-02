"""Explicit, scene-scoped sticker management; never performs native UI actions."""
import json
from pathlib import Path
import re
import time

from sticker_library import PRIVATE, TAGS


def _recent(job, library, store, now):
    scene, person = job.get('username'), job.get('person_id')
    focus = job.get('focus', {}).get('message', {})
    if (job.get('account') != library.account or not scene or not person
            or focus.get('outgoing') is not False or (focus.get('type', 0) & 65535) != 1
            or focus.get('sender') != person
            or (not scene.endswith('@chatroom') and person != scene)):
        return None
    cutoff = min(now, focus.get('timestamp', now))
    rows = store._rows('''SELECT a.*,s.source,s.local_id,s.ts FROM sticker_assets a
        JOIN sticker_sources s ON a.account=s.account AND a.id=s.id
        JOIN sticker_source_evidence e ON s.account=e.account AND s.scene=e.scene
            AND s.source=e.source AND s.local_id=e.local_id AND s.id=e.id
        WHERE a.account=? AND s.scene=? AND e.sender=? AND s.ts>=? AND s.ts<=?
        ORDER BY s.ts DESC,s.local_id DESC''',
        (library.account, scene, person, cutoff-600, cutoff))
    return [row for row in rows if not PRIVATE.search(row['description']) and Path(row['path']).is_file()]


def _status(rows):
    items = {row['id']: row for row in rows}.values()
    counts = {state: 0 for state in ('pending', 'confirmed', 'unconfirmed', 'unsupported')}
    for item in items:
        counts[item['native_state'] if item['native_state'] in counts else 'unconfirmed'] += 1
    return ('当前会话中你最近10分钟的可复用表情：原文件已保存%d；原生收藏已确认%d；'
            '待处理%d；结果未知%d；不支持%d。仅统计已核验来源，不含全部私人归档。' %
            (sum(counts.values()), counts['confirmed'], counts['pending'], counts['unconfirmed'], counts['unsupported']))


def _command_text(job):
    text=job.get('focus_text','').strip()
    if job.get('username','').endswith('@chatroom'):
        text=re.sub(r'^(?:@[^\s\u2005]+[\s\u2005]+)+','',text).strip()
    return text.rstrip('。！？!? ')


def recognizes(job):
    text=_command_text(job)
    return text in ('刚才哪些表情保存了','表情保存状态','刚才没收藏的有哪些','刚才没收藏的继续') or bool(re.fullmatch(r'(?:这张是(.+?)[，,]不是(.+)|把刚才的表情标成(.+))',text))


def handle(job, library, store, now=None):
    """Return an explicit command reply, or None; continuation metadata stays on job.

    sticker_continue_candidates contains only pending, caller-scoped originals with
    no unresolved send. The executor must independently verify source/freshness.
    """
    text = _command_text(job)
    status = text in ('刚才哪些表情保存了', '表情保存状态', '刚才没收藏的有哪些')
    continuation = text == '刚才没收藏的继续'
    correction = re.fullmatch(r'(?:这张是(.+?)[，,]不是(.+)|把刚才的表情标成(.+))', text)
    if not status and not continuation and not correction:
        return None
    rows = _recent(job, library, store, time.time() if now is None else now)
    if rows is None:
        return '无法确认当前账号和发起人的表情来源，请在原会话重新发送表情后再试。'
    if correction:
        tag = correction[1] or correction[3]
        if tag not in TAGS or (correction[2] and correction[2] not in TAGS):
            return '只能修改为已支持的情绪标签，如无奈、生气、开心。'
        # Equal timestamps in different source files cannot establish which came last.
        latest = rows[0] if rows else None
        if not latest or any(row['ts'] == latest['ts'] and row['source'] != latest['source']
                             and row['id'] != latest['id'] for row in rows[1:]):
            return '无法确认你刚才发的哪张表情，请重新发送后再标注。'
        store._write('UPDATE sticker_assets SET tags=?,description=? WHERE account=? AND id=?',
            (json.dumps([tag], ensure_ascii=False), '用户标注为'+tag+'的表情', library.account, latest['id']))
        return '已把你刚才的表情标注为'+tag+'；原生收藏状态保持原记录。'
    if continuation:
        blocked = {row['id'] for row in store._rows(
            "SELECT id FROM sticker_usage WHERE account=? AND state IN ('sending','unconfirmed')", (library.account,))}
        found = {}
        for row in rows:
            if row['native_state'] == 'pending' and row['id'] not in blocked:
                found.setdefault(row['id'], dict(id=row['id'], source=row['source'], local_id=row['local_id'],
                    scene=job['username'], sender=job['person_id'], ts=row['ts']))
        job['sticker_continue_requested'] = True
        job['sticker_continue_candidates'] = list(found.values())
        return _status(rows)+'可继续候选%d，等待执行器核验；结果未知的不会重试。' % len(found)
    return _status(rows)
