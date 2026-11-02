"""Database-driven service. Only this module's main loop touches the GUI."""
import copy
import hashlib
import logging
import os
import signal
import json
import subprocess
import sys
import tempfile
import time
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from event_pipeline import Inbox, Reader
from conversation_memory import People, own_text, person_tag, speaker_label, group_transcript, group_turn, group_focus,remember_group_reply,forget_request
from pipeline_health import PipelineHealth
from reply_plan import freeze_reply,restore_reply
from tasks import Tasks

LOG = logging.getLogger("wechat-autoreply")
STICKER_MEDIA_READY='\x1esticker-media-ready\x1e'

def caption_usable(text):
    return isinstance(text,str) and bool(text.strip()) and not re.search(r'未能读取|无法读取|看不清|无法看清|空白.{0,4}(?:图片|画面)|(?:图片|画面).{0,4}空白|没有可识别|没(?:有)?看(?:清|到)',text)

def sticker_capabilities(job,library,cfg):
    assets=library.current_assets(job) if library else []
    items=library.items() if library else []
    enabled=bool(library and cfg['stickers'].get('enabled'))
    collecting=bool(enabled and cfg['stickers'].get('collect_enabled',True))
    return dict(read_images=bool(cfg['vision'].get('enabled')),save_originals=collecting,
                favorite_native=collecting,send_native=bool(enabled and cfg['stickers'].get('auto_send')),
                ready_count=sum(item['native_state']=='confirmed' for item in items),saved_count=len(items),
                current_original_saved=bool(assets),current_native_confirmed=bool(assets) and all(item['native_state']=='confirmed' for item in assets))


def sticker_should_yield(inbox,job):
    known={row['event_id'] for row in job['batch']}
    stamp=max(row['ts'] for row in job['batch'])
    priority=max(row['priority'] for row in job['batch'])
    with inbox.lock:
        rows=inbox.db.execute("SELECT event_id FROM inbox WHERE account=? AND state IN ('pending','processing') AND priority>=3 AND (priority>? OR ts>?)",(job['account'],priority,stamp)).fetchall()
    return any(row[0] not in known for row in rows)


def sync_sticker(ui,reader,job,library,store,b,should_yield=None):
    from sticker_ui import NativeStickers
    import re
    should_yield=should_yield or (lambda:False)
    if should_yield():return 'skipped'
    pending=library.pending_for(job)
    if not pending:return 'skipped'
    asset=pending[0]
    event='sticker-favorite:'+hashlib.sha256((library.account+asset['id']).encode()).hexdigest()[:24]
    native=NativeStickers(ui,reader,b,should_yield=should_yield)
    for digest,aliases in native.favorite_records():
        if asset['md5'] in aliases:
            library.set_native(asset['id'],digest,'confirmed')
            if store.delivery(event):store.set_delivery_state(event,'confirmed')
            return 'confirmed'
    old=store.delivery(event)
    if old and old['state'] in ('sending','unconfirmed','confirmed'):return old['state']
    store.start_delivery(event,'sticker-library:'+library.account,event,'[收藏表情]',asset['id'])
    def submit():
        if should_yield():return False
        store.set_delivery_state(event,'sending')
        library.set_native(asset['id'],'','unconfirmed')
        return True
    try:
        result=native.favorite_source(asset,job,select_chat,submit)
        if re.fullmatch('[a-f0-9]{32}',result):
            library.set_native(asset['id'],result,'confirmed');store.set_delivery_state(event,'confirmed')
            LOG.info('db_sticker_favorite event=%s result=confirmed',event)
            return 'confirmed'
        if store.delivery(event)['state']=='sending':store.set_delivery_state(event,'unconfirmed')
        LOG.info('db_sticker_favorite event=%s result=%s',event,result)
        return result
    except Exception:
        if store.delivery(event)['state']=='sending':store.set_delivery_state(event,'unconfirmed')
        raise
    finally:
        library.save_favorite_progress(asset['id'],job)
        native.close()


def sticker_requested(job):
    import re
    from sticker_library import NO_STICKER
    text=job.get('focus_text') or job.get('message','')
    return not NO_STICKER.search(text) and bool(re.search(r'(?:发|来|给我发|给我来|回我|回个|回复).{0,10}表情(?:包)?|(?:用|拿).{0,10}表情(?:包)?.{0,10}(?:回复|回我|回应)',text))


def choose_sticker_asset(library,job,reply,cfg,old,explicit,event):
    if old:return library.get(old['reply'])
    options={}
    if job.get('structured_reply'):
        if not job.get('sticker_choice'):return None
        options['semantic_id']=job['sticker_choice']
    return library.choose(job.get('message',''),reply,job['username'],event,explicit=explicit,frequency=cfg.get('stickers',{}),**options)


def send_sticker(ui,reader,job,reply,library,store,b,cfg,explicit=False,should_yield=None):
    from sticker_ui import NativeStickers,sticker_receipt
    from sticker_library import NO_STICKER
    import re
    if not cfg.get('stickers',{}).get('auto_send',False):return 'skipped'
    if NO_STICKER.search(job.get('focus_text') or job.get('message','')):return 'skipped'
    explicit=explicit or sticker_requested(job)
    should_yield=should_yield or (lambda:False)
    if should_yield():return 'skipped'
    event='sticker:'+job['event_id']
    old=store.delivery(event)
    if old and old['state'] in ('confirmed','sending','unconfirmed','failed'):
        return 'confirmed' if old['state']=='confirmed' else 'unconfirmed' if old['state'] in ('sending','unconfirmed') else 'skipped'
    if job['kind']=='group' and job.get('group_reply_mode') not in ('mention','engaged'):return 'skipped'
    if job.get('wav') or not job.get('batch') or fresh_incoming(reader,job) or b.quota_wait(store,job,cfg):return 'skipped'
    asset=choose_sticker_asset(library,job,reply,cfg,old,explicit,event)
    if asset is None:return 'skipped'
    if not explicit and any(asset['md5'] in row['message'].get('sticker_hashes',[]) or asset['native_md5'] in row['message'].get('sticker_hashes',[]) for row in job['batch']):return 'skipped'
    if not select_chat(ui,reader,job,b):return 'skipped'
    sources=[source for source in reader.keys if re.fullmatch(r'message/message_\d+\.db',source)]
    before={source:max((row['local_id'] for row in reader.messages(job['username'],limit=1,source=source)),default=0) for source in sources}
    if not sources:return 'skipped'
    store.start_delivery(event,job['session_key'],event,'[表情包]',asset['id'])
    native=NativeStickers(ui,reader,b,should_yield=should_yield)
    def submit():
        if explicit:store.add_delivery_memory('user:'+event,job['session_key'],'user',job.get('message',''))
        store.set_delivery_state(event,'sending');library.record_send(asset['id'],job['username'],event,'sending');return True
    try:
        def guard():
            name,path=ui.selected_info()
            return bool(not should_yield() and name and path and (b.digest(name.splitlines()[0]),path)==job['expected'][:2] and active_account(b.wechat_session()[0],reader) and not fresh_incoming(reader,job))
        result=native.send(asset,guard,submit)
        if result!='submitted':
            state='unconfirmed' if store.delivery(event)['state']=='sending' else 'failed'
            store.set_delivery_state(event,state)
            LOG.info('db_sticker_send event=%s result=%s',event,result)
            return 'unconfirmed' if state=='unconfirmed' else 'skipped'
        confirmed=False
        for _ in range(20):
            if any(sticker_receipt(reader.messages(job['username'],limit=100,after_id=before[source],source=source),before[source],asset['native_md5']) for source in sources):
                confirmed=True;break
            time.sleep(.25)
        state='confirmed' if confirmed else 'unconfirmed'
        store.set_delivery_state(event,state);library.record_send(asset['id'],job['username'],event,state)
        if confirmed:store.add_delivery_memory(event,job['session_key'],'assistant','[发送了一个'+','.join(json.loads(asset['tags']))+'表情包]')
        LOG.info('db_sticker_send event=%s result=%s',event,state)
        return state
    except Exception:
        state='unconfirmed' if store.delivery(event)['state']=='sending' else 'failed'
        store.set_delivery_state(event,state)
        if state=='unconfirmed':library.record_send(asset['id'],job['username'],event,state)
        raise
    finally:native.close()


def deliver_sticker_only(ui,reader,job,reply,library,store,b,cfg,should_yield=None):
    if reply!=b.agent.STICKER_ONLY or not job.get('sticker_only') or not job.get('sticker_choice'):
        raise ValueError('invalid_sticker_only_reply')
    result=send_sticker(ui,reader,job,'',library,store,b,cfg,explicit=sticker_requested(job),should_yield=should_yield) if library else 'skipped'
    if result=='skipped':job['skip_reason']='sticker_unavailable'
    store.mark_event(job['event_id'])
    return 'unconfirmed' if result=='unconfirmed' else 'done'


def run_due_task(ui,reader,tasks,store,inbox,b,cfg,now=None,health=None,followups=None):
    if inbox.count(reader.account,min_priority=3):return False
    for task in tasks.due(reader.account,now=now):
        job=dict(username=task['username'],title=task['title'],account=reader.account,person_id=task['person_id'],
                 session_key=task['session_key'],event_id='task:'+task['id'],message='[本人创建的到期提醒]',kind=task['kind'],
                 sender=reader.contacts().get(task['person_id'],'') if task['kind']=='group' else '',batch=[],is_mention=task['kind']=='group')
        allowed,_=b.policy.allowed(cfg['policy'],dict(name=job['title'],kind=job['kind'],is_mention=job['is_mention'],engaged=True))
        if not allowed or b.quota_wait(store,job,cfg):continue
        if followups and not followups.can_deliver(task,now=now):continue
        if not tasks.claim(task['id']):continue
        if health:health.note(job,'sending','command')
        try:
            single=copy.deepcopy(cfg);single['delivery']['split_enabled']=False
            body=followups.delivery_text(task) if followups else None
            result=send_reply(ui,reader,job,body or '提醒：'+task['body'],store,b,single)
            tasks.finish(task['id'],'confirmed' if result=='done' else 'pending' if result=='retry' else 'unconfirmed')
            if health:health.note(job,'confirmed' if result=='done' else 'retry' if result=='retry' else 'uncertain','sent' if result=='done' else 'send_retry' if result=='retry' else 'send_unknown')
        except Exception:
            tasks.finish(task['id'],'unconfirmed')
            if health:health.note(job,'uncertain','send_unknown')
            LOG.warning('db_task_delivery_unknown')
        return True
    return False


def background_allowed(inbox, account, due, now, busy=False):
    if now < due or inbox.count(account,min_priority=3):
        return False
    return not busy or now-due >= 120


def execute_command(ui,reader,job,store,b,cfg):
    """Freeze command outcome independently of the chat reply delivery."""
    event='action:'+job['event_id']
    old=store.delivery(event)
    if old:
        return old['reply'] if old['state'] in ('confirmed','failed') else '这项操作已提交，结果未确认；我不会重复执行。'
    command=job['command']
    before=[]
    relay_job=None
    if command in ('like','comment','replybox'):
        before=reader.moments(limit=100)
        if command in ('like','comment'):
            candidates=sorted((p for p in before if p['username']==job['username']),key=lambda p:p.get('timestamp',0),reverse=True)
            if candidates:
                job['moment_target_id']=candidates[0]['id']
                job['moment_target_text']=candidates[0].get('text','')
    elif command=='relay':
        matches=[s for s in reader.sessions(limit=10000) if s['title']==job['relay_target']]
        if len(matches)!=1:
            return '没有唯一匹配的联系人，未发送。'
        relay_job=dict(job,username=matches[0]['username'])
        before=max((r['local_id'] for r in reader.messages(relay_job['username'],limit=1)),default=0)
    store.start_delivery(event,job['session_key'],event,job['message'],'操作结果未确认；我不会重复执行。')
    store.set_delivery_state(event,'sending')
    state='unconfirmed'
    reply='操作结果未确认；我不会重复执行。'
    try:
        handler=dict(replybox=getattr(b,'run_replybox_command',None),relay=getattr(b,'run_relay_command',None),
                     inbox=getattr(b,'run_inbox_query',None),read=getattr(b,'run_relay_read',None)).get(command,b.run_moment_command)
        if command in ('like','comment') and not job.get('moment_target_id'):
            job['action_result']='missing'
            raw='没有找到可核验的目标朋友圈，未发送。'
        else:
            raw=handler(ui,job) if command=='replybox' else handler(ui,job,cfg)
        if command in ('inbox','read'):
            state,reply='confirmed',raw
        elif command in ('like','comment') and job.get('action_result') in ('missing','no_button','empty','not_typed'):
            state,reply='failed',raw
        else:
            target=next((p for p in sorted(before,key=lambda p:p.get('timestamp',0),reverse=True) if p['username']==job['username']),None) if isinstance(before,list) else None
            for _ in range(12):
                if command=='relay':
                    confirmed=receipt(reader,relay_job,before,job['relay_text'])
                else:
                    after=reader.moments(limit=100)
                    own_before={(p['id'],c['id']) for p in before for c in p.get('thread',[]) if c['username']==reader.account}
                    confirmed=False
                    for p in after:
                        if command!='replybox' and (not target or p['id']!=target['id'] or p['username']!=job['username']):
                            continue
                        if command=='like':
                            confirmed=reader.account in p.get('likes',[])
                        else:
                            text=job.get('replybox_text') if command=='replybox' else job.get('comment_text')
                            confirmed=any(c['username']==reader.account and not c.get('deleted') and c.get('id') and (p['id'],c['id']) not in own_before and c['text'].strip()==str(text or '').strip() for c in p.get('thread',[]))
                        if confirmed:
                            break
                if confirmed:
                    state='confirmed'
                    reply={'like':'已确认点赞。','comment':'已确认评论发出。','replybox':'已确认回复评论。','relay':'已确认消息发给对方。'}[command]
                    break
                time.sleep(0.25)
    except Exception as exc:
        LOG.warning('db_command_unknown event=%s type=%s',event,type(exc).__name__)
    store._write('UPDATE outbox SET state=?,reply=?,updated_at=? WHERE delivery_id=?',(state,reply,time.time(),event))
    LOG.info('db_command_result event=%s command=%s state=%s',event,command,state)
    return reply


def interruptible_model(pool, call, should_yield):
    def invoke(*args):
        future = pool.submit(call, *args)
        while not future.done():
            if should_yield():
                future.cancel()
                return ""
            time.sleep(0.2)
        return "" if should_yield() else future.result()
    return invoke


def synthesize_wav(spoken, path):
    # Isolate cloud TTS so a stuck SDK cannot occupy a model worker indefinitely.
    code = "import json,sys,voice; p=json.load(sys.stdin); r=voice.synthesize(p['text'],out=p['path']); sys.exit(0 if r else 1)"
    result = subprocess.run([sys.executable, "-c", code], input=json.dumps(dict(text=spoken, path=path)),
                            text=True, capture_output=True, timeout=30)
    return path if result.returncode == 0 and os.path.getsize(path) > 44 else None


def recover_abandoned(inbox, store, job=None, batch=None):
    if job is not None:
        submitted = store._rows("SELECT state FROM outbox WHERE event_id=? AND state IN ('sending','confirmed','unconfirmed')", (job["event_id"],))
        inbox.complete(job["batch"], "unconfirmed" if submitted else "retry")
    elif batch is not None:
        inbox.complete(batch, "retry")


def active_account(pid, reader):
    from pathlib import Path
    expected = str(reader.db_dir.parent.resolve()) + os.sep
    matched = False
    for entry in Path(f"/proc/{pid}/fd").iterdir():
        try:
            path = os.readlink(entry)
        except OSError:
            continue
        if "/xwechat_files/" in path and "/db_storage/" in path:
            if not path.startswith(expected):
                return False
            matched = True
    return matched


def fresh_incoming(reader,job):
    cursors={}
    for row in job.get('batch',[]):
        cursors[row['source']]=max(cursors.get(row['source'],0),row['message']['local_id'])
    return [m for source,local in cursors.items()
            for m in reader.messages(job['username'],limit=200,after_id=local,source=source) if not m['outgoing']]


def interrupts_parts(message,job,b,cfg):
    if job.get('kind')!='group':
        return True
    return (message.get('sender')==job.get('person_id') or message.get('quote_sender')==job.get('account')
            or b.mentioned_self(message.get('text',''),b.policy_self_names(cfg)))


def stale_group_reply(reader,job,b,cfg):
    return (job.get('kind')=='group' and not job.get('is_mention')
            and job.get('group_reply_mode') in ('engaged','interject','proactive')
            and any(interrupts_parts(message,job,b,cfg) for message in fresh_incoming(reader,job)))


def obsolete_reply(reader,job,inbox,store,b,cfg):
    """Only discard an untouched model draft after its replacement input is durable."""
    if job.get('command') or job.get('memory_response') or job.get('command_response'):
        return ''
    event=job['event_id']
    if store._rows("SELECT 1 FROM outbox WHERE event_id=? OR event_id LIKE ? OR event_id IN (?,?) LIMIT 1",
                   (event,event+':part:%','action:'+event,'sticker:'+event)):
        return ''
    cursors={}
    for row in job.get('batch',[]):
        cursors[row['source']]=max(cursors.get(row['source'],0),row['message']['local_id'])
    relevant=[]
    for source,local in cursors.items():
        for message in reader.messages(job['username'],limit=200,after_id=local,source=source):
            if message.get('outgoing'):continue
            # Strict-mention replacements must retain their own permission to answer.
            if (job.get('kind')=='group' and job.get('is_mention') and cfg.get('policy',{}).get('group_strict_mention')
                    and message.get('quote_sender')!=job.get('account')
                    and not b.mentioned_self(message.get('text',''),b.policy_self_names(cfg))):continue
            spontaneous=job.get('kind')=='group' and not job.get('is_mention') and job.get('group_reply_mode') in ('engaged','interject','proactive')
            if spontaneous or interrupts_parts(message,job,b,cfg):relevant.append((source,message['local_id']))
    if not relevant:return ''
    with inbox.lock:
        durable=False
        for source,local in relevant:
            if inbox.db.execute("SELECT 1 FROM inbox WHERE account=? AND username=? AND source=? AND local_id=?",
                    (job['account'],job['username'],source,local)).fetchone():durable=True;continue
            cursor=inbox.cursor(job['account'],job['username'],source)
            # Already-consumed rows may have been excluded by the initial history cutoff.
            if cursor is None or cursor<local:return 'wait'
    return 'obsolete' if durable else ''


def build_job(batch, inbox, b, cfg, account):
    head, latest = batch[0], batch[-1]
    user = head["username"]
    group = user.endswith("@chatroom")
    explicit=[row for row in batch if ((row['message'].get('type',1)&65535)==1 and b.mentioned_self(row['message'].get('text',''),b.policy_self_names(cfg))) or row['message'].get('quote_sender')==account]
    focus=explicit[-1] if group and explicit else latest
    parts = []
    for row in batch:
        message = row["message"]
        kind = message.get("type", 1) & 0xFFFF
        text = b.clean_db_text(own_text(message))
        if kind == 3:
            text = "[图片]"
        elif kind == 47:
            text = "[表情包]"
        elif kind == 34:
            text = "[语音消息，尚未转写]"
        elif kind != 1:
            text = "[暂不支持的消息类型]"
        if group:
            text='[成员#'+person_tag(account,str(message.get('sender','unknown')))+'] '+text
        parts.append(text)
    raw_latest = b.clean_db_text(own_text(focus['message']))
    return dict(event_id=head["batch_id"], username=user, account=account,
                session_key=inbox.memory_key(account, user), identity=user, title=head["title"],
                message=b.build_batch_message(parts), batch_count=len(batch), batch_digest=b.digest("\n".join(parts)),
                kind="group" if group else "private", sender="", is_mention=bool(group and explicit),
                is_media=any((r["message"].get("type", 1) & 0xFFFF) != 1 for r in batch),
                visible_context="", confirmed="database", command="", comment_text="", relay_target="",
                relay_text="", read_target="", replybox_text="", db_last_local=0,
                voice_mode=bool(b.voice_module and b.voice_module.requested(raw_latest)),
                force_reply=False, batch=batch, started=time.monotonic(), focus=focus,
                person_id=str(focus['message'].get('sender','')) if group else user,
                focus_text=raw_latest,quote_sender=focus['message'].get('quote_sender',''))


def group_reply_relevant(job,active,eligible,interject,cfg):
    return bool(job.get('is_mention') or active or interject or (eligible and job.get('title') in cfg['policy'].get('group_always_reply',[])))


def choose_command(job, b, cfg, owner):
    if job["kind"] != "private":
        return
    raw = b.clean_db_text(job["batch"][-1]["message"].get("text", ""))
    replybox = b.parse_replybox_command(raw)
    if replybox and owner:
        job.update(command="replybox", replybox_text=replybox)
    elif owner and b.parse_inbox_command(raw):
        job["command"] = "inbox"
    elif owner and b.parse_read_command(raw):
        job.update(command="read", read_target=b.parse_read_command(raw))
    elif owner and cfg["relay"]["enabled"] and b.parse_relay_command(raw):
        target, body = b.parse_relay_command(raw)
        job.update(command="relay", relay_target=target, relay_text=body)
    elif cfg["moments"]["enabled"] and b.parse_moment_command(raw):
        action, comment = b.parse_moment_command(raw)
        job.update(command=action, comment_text=comment)


def select_chat(ui, reader, job, b):
    session = b.wechat_session()
    if not session or not active_account(session[0], reader):
        return False
    sessions = reader.sessions(limit=10000)
    matches = [s for s in sessions if s["username"] == job["username"]]
    if not matches:
        return False
    title = matches[0]["title"]
    if sum(s["title"] == title for s in sessions) != 1:
        LOG.warning("db_target_ambiguous event=%s", job["event_id"])
        return False
    if not ui.activate():
        return False
    ui._discover()
    selected, path = ui.selected_info()
    latest = ui.latest_message()
    if selected and selected.splitlines()[0].strip() == title and latest:
        if not active_account(session[0], reader):
            return False
        job.update(title=title, path=path, expected=(b.digest(title), path, b.digest(latest[0].strip())))
        return True
    bounds = b.component_rect(ui.left_list, ui.pyatspi)
    pointer = ui.pointer()
    pointer.move(bounds.x + bounds.width // 2, bounds.y + bounds.height // 2)
    if not any(name.splitlines()[0].strip() == title for name, _, _ in ui.rows(ui.left_list)):
        pointer.scroll(40, up=True)
    previous = None
    for _ in range(50):
        ui._discover()
        rows = ui.rows(ui.left_list)
        found = [(name, row) for name, _, row in rows if name.splitlines()[0].strip() == title]
        if len(found) == 1:
            name, row = found[0]
            path = str(getattr(row, "path", ""))
            if not ui.click_row(b.digest(name), b.row_identity(path, title)):
                return False
            time.sleep(0.3)
            ui._discover()
            selected, path = ui.selected_info()
            latest = ui.latest_message()
            if selected and selected.splitlines()[0].strip() == title and latest:
                if not active_account(session[0], reader):
                    return False
                job.update(title=title, path=path, expected=(b.digest(title), path, b.digest(latest[0].strip())))
                return True
            return False
        signature = tuple(name for name, _, _ in rows)
        if signature == previous:
            break
        previous = signature
        pointer.move(bounds.x + bounds.width // 2, bounds.y + bounds.height // 2)
        pointer.scroll(5)
    return False


def receipt(reader, job, before, reply, voice=False):
    """A new outgoing row with a server ID is required; old identical text cannot match."""
    for row in reader.messages(job["username"], limit=200, after_id=before):
        kind = row["type"] & 0xFFFF
        text = row["text"].strip()
        variants = {reply.strip()}
        if job.get("sender"):
            variants.add("@" + job["sender"] + " " + reply.strip())
        mention_match = bool(job.get("sender") and text.startswith("@" + job["sender"]) and
                             text[len(job["sender"]) + 1:][:1].isspace() and
                             text[len(job["sender"]) + 1:].lstrip() == reply.strip())
        matches = kind == 34 if voice else kind == 1 and (text in variants or mention_match)
        if row["outgoing"] and matches and row.get("server_id") not in (None, "", "0", 0):
            return True
    return False


def _send_reply_single(ui, reader, job, reply, store, b, cfg):
    delivery = b.delivery_key(job["session_key"], job["event_id"], reply)
    old = store.delivery(delivery)
    if old and old["state"] in ("confirmed", "unconfirmed", "sending"):
        if job.get("part") and old["state"] == "confirmed":
            store.add_delivery_memory("assistant:" + delivery, job["session_key"], "assistant", reply)
        return "done" if old["state"] == "confirmed" else "unconfirmed"
    if not select_chat(ui, reader, job, b):
        return "retry"
    before = max((r["local_id"] for r in reader.messages(job["username"], limit=1)), default=0)
    store.start_delivery(delivery, job["session_key"], job["event_id"], job["message"], reply)
    voice = bool(job.get("wav"))
    store.set_delivery_state(delivery, "sending")
    try:
        if voice:
            submitted = b.voice_module.send(ui, job["wav"], guard=lambda: b.same_chat(ui, job["expected"]) and active_account(b.wechat_session()[0], reader))
            result = "uncertain" if submitted else "retry"
        else:
            mention = job.get("sender") if job["kind"] == "group" and cfg["policy"].get("group_mention_reply") else None
            result = ui.send(reply, job["expected"], mention=mention, single_submit=True)
    except Exception as exc:
        LOG.warning("db_send_unknown=%s event=%s", type(exc).__name__, job["event_id"])
        result = "uncertain"
    if result == "retry":
        store.set_delivery_state(delivery, "queued")
        return "retry"
    confirmed = False
    for _ in range(16):
        try:
            if receipt(reader, job, before, reply, voice):
                confirmed = True
                break
        except Exception:
            pass
        time.sleep(0.25)
    store.set_delivery_state(delivery, "confirmed" if confirmed else "unconfirmed")
    store.mark_event(job["event_id"])
    if not job.get("part"):
        store.add_message(job["session_key"], "user", job["message"])
        if confirmed:
            store.add_message(job["session_key"], "assistant", reply)
        else:
            store.add_message(job["session_key"], "user", "[上一轮回复发送结果未确认，不要声称对方已收到。]")
        store.refresh_daily_digest(job["session_key"])
    elif confirmed:
        store.add_delivery_memory("assistant:" + delivery, job["session_key"], "assistant", reply)
    LOG.info("db_reply_%s event=%s delivery=%s kind=%s", "confirmed" if confirmed else "unconfirmed",
             job["event_id"], delivery, "voice" if voice else "text")
    return "done" if confirmed else "unconfirmed"


def send_reply(ui, reader, job, reply, store, b, cfg):
    existing = store._rows("SELECT * FROM outbox WHERE session_key=? AND event_id=? LIMIT 1", (job["session_key"], job["event_id"]))
    parent = existing[0] if existing else None
    frozen_parts = None
    if parent:
        try:
            plan = json.loads(parent["message"])
            if isinstance(plan, list) and len(plan) > 1 and all(isinstance(p,str) for p in plan):
                frozen_parts = plan
        except ValueError:
            pass
    if frozen_parts:
        job = dict(job)
        job.pop("wav", None)
        reply = parent["reply"]
    elif job.get("wav") or not hasattr(b, "split_reply_bubbles"):
        return _send_reply_single(ui, reader, job, reply, store, b, cfg)
    parent_id = parent["delivery_id"] if frozen_parts else b.delivery_key(job["session_key"], job["event_id"], reply)
    parent = store.delivery(parent_id)
    if parent and parent["state"] in ("confirmed", "unconfirmed", "sending"):
        return "done" if parent["state"] == "confirmed" else "unconfirmed"
    if parent:
        try:
            parts = json.loads(parent["message"])
            if not isinstance(parts, list) or not all(isinstance(p, str) for p in parts):
                parts = [reply]
        except ValueError:
            parts = [reply]
    else:
        parts = b.split_reply_bubbles(reply, job["session_key"], store, cfg)
    if len(parts) < 2:
        return _send_reply_single(ui, reader, job, reply, store, b, cfg)
    # Freeze the complete split plan in one durable insert before submitting any part.
    store.start_delivery(parent_id, job["session_key"], job["event_id"], json.dumps(parts, ensure_ascii=False), reply)
    store.add_delivery_memory("user:" + parent_id, job["session_key"], "user", job["message"])
    confirmed = []
    result = "done"
    for index, part in enumerate(parts):
        if index and job.get("batch"):
            if any(interrupts_parts(m,job,b,cfg) for m in fresh_incoming(reader,job)):
                LOG.info("db_reply_parts_interrupted event=%s confirmed=%d", job["event_id"], len(confirmed))
                break
        child = dict(job, event_id=job["event_id"] + ":part:" + str(index), part=True)
        if index:
            child["sender"] = ""
        previous_part = store.delivery(b.delivery_key(child["session_key"], child["event_id"], part))
        wait = 0 if previous_part and previous_part["state"] == "confirmed" else b.quota_wait(store, child, cfg)
        if wait:
            job["defer_seconds"] = wait
            store.set_delivery_state(parent_id, "queued")
            return "defer"
        store.set_delivery_state(parent_id, "sending")
        result = _send_reply_single(ui, reader, child, part, store, b, cfg)
        if result != "done":
            store.set_delivery_state(parent_id, "queued" if result == "retry" else "unconfirmed")
            break
        confirmed.append(part)
        store.set_delivery_state(parent_id, "queued")
        if index + 1 < len(parts):
            time.sleep(0.6)
    if result == "retry":
        return result
    store.set_delivery_state(parent_id, "confirmed" if result == "done" else "unconfirmed")
    store.mark_event(job["event_id"])
    if result != "done":
        store.add_delivery_memory("unknown:" + parent_id, job["session_key"], "user", "[后续消息发送结果未确认，剩余部分已取消，不要声称全部送达。]")
    store.refresh_daily_digest(job["session_key"])
    LOG.info("db_reply_parts event=%s confirmed=%d total=%d result=%s", job["event_id"], len(confirmed), len(parts), result)
    return result


def collect_sticker(stickers,reader,scene,row,vision_fn,raw=None,paths=None):
    import sticker_media
    from sticker_library import CLASSIFY_PROMPT,parse_reusable
    if not vision_fn:return
    message=row['message']
    hashes=message.get('sticker_hashes') or sticker_media.metadata(message.get('text',''))
    raw=raw or sticker_media.original_bytes(reader.db_dir.parent,reader.account,hashes)
    if not raw:return
    existing=stickers.get(hashlib.sha256(raw).hexdigest())
    if existing:
        stickers.collect_message(reader,scene,row,existing['description'],raw=raw,tags=json.loads(existing['tags']))
        return existing['id']
    prompt=CLASSIFY_PROMPT
    if paths:
        description=vision_fn(paths,prompt) or ''
    else:
        with tempfile.TemporaryDirectory(prefix='sticker-library-') as root:
            frames=sticker_media.frames(raw,root)
            description=vision_fn(frames,prompt) if frames else ''
    record=parse_reusable(description)
    if record:return stickers.collect_message(reader,scene,row,record['description'],raw=raw,tags=record['tags'])

def collect_queued_sticker(job,reader,library,vision_fn):
    import sticker_media
    row=job['batch'][0];m=row['message']
    raw=sticker_media.original_bytes(reader.db_dir.parent,reader.account,m.get('sticker_hashes',[]))
    if not raw:return 'retry'
    identifier=collect_sticker(library,reader,job['username'],row,vision_fn,raw=raw)
    return 'done' if identifier else 'rejected'

def maintain_native_stickers(ui,reader,library,store,b,attempts,now,should_yield):
    from sticker_ui import NativeStickers
    if should_yield():return
    native=NativeStickers(ui,reader,b,should_yield=should_yield)
    records=native.favorite_records()
    for asset in library.items():
        if asset['native_state']=='confirmed':continue
        match=next((digest for digest,aliases in records if asset['md5'] in aliases),None)
        if match:library.set_native(asset['id'],match,'confirmed')
    for job in library.favorite_jobs():
        if should_yield():return
        if now-attempts.get(job['asset_id'],-1000)<300:continue
        attempts[job['asset_id']]=now
        return sync_sticker(ui,reader,job,library,store,b,should_yield)


def task_command_reply(tasks,job):
    from tasks import is_composite_request
    job['task_handler']=lambda request:tasks.create_from_tool(job,request)
    return None if is_composite_request(job) else tasks.handle(job)


def verified_management_owner(job,owners,cfg):
    identifiers=cfg['runtime'].get('owner_ids',[])
    return job.get('kind')=='private' and (job['username'] in identifiers if identifiers else owners.get(job['username']) in {t.casefold() for t in cfg['relay']['allowed_titles']})


def capability_reply(job,reader,store,cfg,controls=None,owner=False,stickers=None,followups=None):
    from runtime_controls import is_command
    focus=job.get('focus',{}).get('message',{})
    if (job.get('account')!=getattr(reader,'account',None) or focus.get('outgoing') is not False
            or focus.get('type')!=1 or focus.get('sender')!=job.get('person_id')
            or focus.get('quote_sender') or focus.get('quote_text')):return None
    if job.get('kind')=='group' and not job.get('is_mention') and job.get('group_reply_mode')!='engaged':return None
    command_text=own_text(focus).strip()
    if job.get('kind')=='group':
        for name in job.get('self_names',[]):command_text=re.sub(r'^@'+re.escape(name)+r'[\s\u2005]+','',command_text,count=1)
    if controls and is_command(command_text):
        if not owner:return '管理设置只接受已验证的主人账号私聊指令。'
        return controls.handle(job,owner) or '无法验证这条管理指令的原始来源，设置未更改。'
    if stickers:
        import sticker_commands
        if sticker_commands.recognizes(job):backfill_sticker_evidence(job,reader,stickers,store)
        reply=sticker_commands.handle(job,stickers,store)
        if reply is not None:return reply
    if followups:
        reply=followups.handle(job)
        if reply is not None:return reply
        offer=followups.observe(job)
        if offer:
            job['conversation_context']=job.get('conversation_context','')+'\n本会话可选择的跟进提议（尚未获得同意，不是已安排的任务）：'+offer
    return None


def backfill_sticker_evidence(job,reader,library,store):
    cutoff=min(time.time(),job['focus']['message']['timestamp'])
    rows=store._rows('SELECT s.*,a.md5 FROM sticker_sources s JOIN sticker_assets a ON a.account=s.account AND a.id=s.id WHERE s.account=? AND s.scene=? AND s.ts>=? AND s.ts<=? AND NOT EXISTS (SELECT 1 FROM sticker_source_evidence e WHERE e.account=s.account AND e.scene=s.scene AND e.source=s.source AND e.local_id=s.local_id) ORDER BY s.ts DESC LIMIT 40',(library.account,job['username'],cutoff-600,cutoff))
    sources={}
    for row in rows:
        if row['source'] not in sources:sources[row['source']]=reader.messages(job['username'],limit=200,source=row['source'])
        message=next((m for m in sources[row['source']] if m['local_id']==row['local_id']),None)
        if (message and not message.get('outgoing') and (message.get('type',0)&65535)==47
                and message.get('sender')==job['person_id'] and message.get('timestamp')==row['ts']
                and row['md5'] in message.get('sticker_hashes',[])):
            store._write('INSERT OR REPLACE INTO sticker_source_evidence VALUES(?,?,?,?,?,?)',(library.account,job['username'],row['source'],row['local_id'],job['person_id'],row['id']))


def continue_sticker_request(ui,reader,job,library,store,b,should_yield):
    import sticker_commands
    candidates=job.get('sticker_continue_candidates',[])
    if not candidates:return sticker_commands.handle(job,library,store)
    candidate=candidates[0];asset=library.get(candidate['id'])
    messages=reader.messages(job['username'],limit=200,source=candidate['source'])
    message=next((m for m in messages if m['local_id']==candidate['local_id']),None)
    if (not asset or not message or message.get('outgoing') or message.get('sender')!=job['person_id']
            or (message.get('type',0)&65535)!=47 or asset['md5'] not in message.get('sticker_hashes',[])):
        return '未能重新核验该表情的原消息，尚未进行收藏。'
    action=dict(job,batch=[dict(source=candidate['source'],message=message)])
    result=sync_sticker(ui,reader,action,library,store,b,should_yield=should_yield)
    status=sticker_commands.handle(job,library,store)
    if result=='confirmed':return status+'刚才最新的这张已确认收藏；其余按各自状态核对。'
    if result in ('sending','unconfirmed'):return status+'这张收藏结果尚未确认，不会重复点击。'
    if result=='source_not_latest':return status+'该图已不是最新入站媒体，请重发后再收藏。'
    return status+'本次尚未完成新的原生收藏。'


def normalize_forget_messages(messages,b,cfg):
    result=[]
    for message in messages:
        text=own_text(message)
        if not message.get('outgoing') and not message.get('quote_sender') and (message.get('type',1)&65535)==1:
            for name in sorted(b.policy_self_names(cfg),key=len,reverse=True):
                trimmed=re.sub(r'^@'+re.escape(name)+r'[\s\u2005]+','',text,count=1) if name else text
                if trimmed!=text and forget_request(trimmed):
                    message=dict(message,text=trimmed);break
        result.append(message)
    return result


def enrich_intelligence(job,cfg,people,feedback=None,selector=None):
    job['intelligence_enabled']=bool(cfg['quality'].get('intelligence_enabled'))
    if feedback and cfg.get('learning',{}).get('enabled'):
        feedback.observe(job)
        job['feedback_context']=feedback.context(job)
        job['reply_preferences']=feedback.preferences(job)
    if selector and people and cfg['memory'].get('semantic_enabled') and not job.get('memory_response'):
        try:context=selector(job,people)
        except Exception as exc:
            LOG.warning('db_semantic_fallback=%s',type(exc).__name__);context=''
        if context:
            job['conversation_context']=job.get('conversation_context','')+'\n'+context[:1800]
            if job.get('kind')=='group':job['group_focus']=job.get('group_focus','')+'\n'+context[:1400]


def semantic_reply_current(job,people):
    sources=job.get('semantic_sources')
    if not sources:return True
    current=people.semantic_candidates(job['account'],job['person_id'],job['username'],job['focus_text'],now=time.time())
    return set(sources)<={row['id'] for row in current}


def build_semantic_selector(cfg,api_key,store,b,metrics=None):
    from semantic_memory import semantic_recall,should_recall
    class MeteredClient:
        def __init__(self):
            self.client=b.agent.ModelClient(cfg['api']['url'],cfg['api']['model'],api_key,timeout=8,max_tokens=256)
        def chat(self,*args,**kwargs):
            result=self.client.chat(*args,**kwargs)
            try:
                store.add_usage(result.get('tokens_in',0),result.get('tokens_out',0))
                if metrics:metrics.event('semantic_usage',tokens_in=result.get('tokens_in',0),tokens_out=result.get('tokens_out',0),cached=result.get('cached_in',0))
            except Exception:pass
            return result
    def select(job,people):
        if not should_recall(job):return ''
        stamp=job.get('focus',{}).get('message',{}).get('timestamp') or time.time()
        args=(job['account'],job['person_id'],job['username'],job['focus_text'])
        candidates=people.semantic_candidates(*args,now=stamp)
        ids=semantic_recall(MeteredClient(),job['focus_text'],candidates)
        context=people.semantic_context(*args,ids,now=stamp) if ids else ''
        job['semantic_sources']=ids if context else []
        return context
    return select


def prepare_model(job, reader, responder, vision_fn, b, cfg, people=None, stickers=None,health=None,feedback=None,semantic_selector=None):
    cached = restore_reply(job,job["batch"][0].get("reply"))
    if not cached and not job.get('media_prepared'):enrich_intelligence(job,cfg,people,feedback,semantic_selector)
    parts = []
    for row in ([] if cached or job.get('media_prepared') else job["batch"]):
        message = row["message"]
        kind = message.get("type", 1) & 0xFFFF
        if kind in (3, 47):
            if health:health.note(job,'media','media_read')
            observed=people.observation(job['account'],job['username'],row['source'],message) if people else ''
            if caption_usable(observed):
                if kind==47 and stickers and cfg['stickers'].get('collect_enabled',True):
                    try:collect_sticker(stickers,reader,job['username'],row,vision_fn)
                    except Exception as exc:LOG.warning('db_sticker_collect_failed=%s',type(exc).__name__)
                parts.append('[过去已核验的本消息图片/表情观察：'+observed[:600]+']')
                if job.get('kind')=='group':
                    parts[-1]='['+speaker_label(job['account'],str(message.get('sender','')),job.get('sender_names',{}))+'] '+parts[-1]
                continue
            description = ""
            path = None
            paths = []
            media_dir = None
            raw = None
            try:
                if kind == 47:
                    import sticker_media
                    hashes = message.get("sticker_hashes") or sticker_media.metadata(message.get("text", ""))
                    raw = sticker_media.original_bytes(reader.db_dir.parent, reader.account, hashes)
                    if raw:
                        media_dir = tempfile.TemporaryDirectory(prefix="wechat-sticker-")
                        paths = sticker_media.frames(raw, media_dir.name)
                        if paths:
                            LOG.info("db_sticker_original event=%s frames=%d", job["event_id"], len(paths))
                if kind == 3 and message.get("image_md5"):
                    media_dir = tempfile.TemporaryDirectory(prefix="wechat-media-")
                    path = reader.decode_image(message["image_md5"], out_dir=media_dir.name, server_id=message.get('server_id'))
                native=bool(paths or path)
                if not paths and row["event_id"] == job.get("capture_event"):
                    path = path or job.get("capture")
                    paths = job.get("captures", [])
                paths = [path] if path else paths
                if kind == 47 and paths and media_dir is None:
                    from PIL import Image
                    import sticker_media
                    accepted=[]
                    for frame_path in paths:
                        with Image.open(frame_path) as image:
                            if sticker_media.meaningful(image):
                                accepted.append(frame_path)
                    paths=accepted
                LOG.info('db_media_source event=%s kind=%d source=%s frames=%d',job['event_id'],kind,
                         'original' if native and paths else 'capture' if paths else 'missing',len(paths))
                if paths and vision_fn:
                    context = job.get("visual_context", "")
                    prompt = "这些是本条图片或动态表情的真实画面。先独立观察主体、颜色、文字、动作和情绪，再结合前文说明可能在接什么梗。历史聊天不能代替当前画面，之前看不清不代表这次也看不清；区分画面事实与推测。\n仅供理解语境的聊天前文：\n" + context[:1500]
                    description = vision_fn(paths, prompt) or ""
                    if people and caption_usable(description):
                        people.observe(job['account'],job['username'],row['source'],message,description,'original' if native else 'capture')
                    if kind==47 and raw and stickers and description and cfg['stickers'].get('collect_enabled',True):
                        collect_sticker(stickers,reader,job['username'],row,vision_fn,raw=raw,paths=paths)
            except Exception as exc:
                LOG.warning("db_media_failed=%s event=%s", type(exc).__name__, job["event_id"])
                if health:health.note(job,'media','media_unreadable')
            finally:
                for path in paths:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                if media_dir:
                    media_dir.cleanup()
            parts.append("[图片/表情内容：%s]" % (description[:600] or "未能读取，不要猜测内容"))
        elif kind == 34:
            import voice_input
            phases={}
            def phase(name,seconds):
                phases[name]=seconds
                if health:health.note_phase(job,name,seconds)
            transcript=voice_input.transcribe_message(reader,dict(message,account=job['account'],chat_username=job['username'],source=row['source']),cfg,phase_callback=phase)
            if not transcript and health:health.note(job,'media','voice_timeout' if phases.get('worker',0)>=cfg.get('voice_input',{}).get('timeout',15) else 'voice_unavailable')
            parts.append('[本条语音转写，可能有识别误差：'+transcript+']' if transcript else '[本条语音未能转写，不猜测内容]')
            if transcript:
                job['voice_transcribed']=True;job.pop('media_fallback',None)
            if transcript and row is job.get('focus'):
                job['focus_text']=transcript
            if not transcript and not job.get('voice_transcribed') and all((r['message'].get('type',1)&65535)==34 for r in job['batch']):
                job['media_fallback']='这条语音暂时未能转写，可以换段短语音或发文字给我。'
        elif kind == 1:
            parts.append(b.clean_db_text(own_text(message)))
        else:
            parts.append("[收到一条未能读取的非文字消息，不要假装已经看懂或听懂]")
        if job.get("kind") == "group":
            label=speaker_label(job.get('account',''),str(message.get('sender','')),job.get('sender_names',{}))
            if message.get('quote_sender'):
                valid=not people or people.source_valid(job['account'],job['username'],row['source'],message,now=job.get('focus',{}).get('message',{}).get('timestamp'))
                if valid:parts[-1]='[引用 '+speaker_label(job.get('account',''),message['quote_sender'],job.get('sender_names',{}))+' 的话 '+json.dumps(message.get('quote_text','')[:120],ensure_ascii=False)+'] '+parts[-1]
                else:parts[-1]='[引用来源已清除或失效，不恢复引用内容] '+parts[-1]
            parts[-1] = "[" + label + "] " + parts[-1]
    if parts:
        job["message"] = b.build_batch_message(parts)
    if not cached:
        job['media_prepared']=True
        if stickers and cfg['stickers'].get('collect_enabled',True) and cfg['stickers'].get('prepare_before_reply') and not job.get('favorites_attempted') and stickers.pending_for(job):
            return STICKER_MEDIA_READY
        if cfg['stickers'].get('capabilities_enabled'):job['sticker_capabilities']=sticker_capabilities(job,stickers,cfg)
    if not cached:
        job['structured_reply']=bool(cfg['quality'].get('structured_reply'))
        job['sticker_candidates']=[]
        allow_sticker=job.get('reply_preferences',{}).get('emoji')!='none' or sticker_requested(job)
        if stickers and allow_sticker and cfg['stickers'].get('auto_send') and (job['kind']=='private' or job.get('group_reply_mode') in ('mention','engaged')) and not job['voice_mode']:
            job['sticker_candidates']=stickers.candidates(job.get('message',''),job['username'],'sticker:'+job['event_id'],explicit=sticker_requested(job),frequency=cfg['stickers'])
    if health:health.note(job,'model','generating')
    model_started=time.monotonic()
    reply = cached or job.get('memory_response') or job.get('media_fallback') or responder(job)
    if health:health.note(job,'ready','command' if job.get('memory_response') else 'waiting',duration_ms=(time.monotonic()-model_started)*1000)
    if not reply:
        raise RuntimeError("empty_reply")
    if job["force_reply"] and b.is_silent_reply(reply):
        reply = "在呢，怎么啦？"
    if job["voice_mode"] and not job.get('sticker_only') and not job.get("text_started") and b.voice_module and cfg["voice"]["enabled"] and not (
            cfg["voice"].get("allow_private_only") and job["kind"] == "group") and not b.is_silent_reply(reply):
        fd, path = tempfile.mkstemp(prefix="wechat-tts-", suffix=".wav")
        os.close(fd)
        try:
            spoken = b.voice_module.clean_spoken(reply)[:min(140, cfg["voice"].get("max_chars", 140))]
            job["wav"] = synthesize_wav(spoken, path)
            if job.get("wav"):
                reply = spoken
        except Exception as exc:
            LOG.warning("db_tts_failed=%s", type(exc).__name__)
        if not job.get("wav"):
            os.unlink(path)
            try:
                os.unlink(path + ".mp3")
            except OSError:
                pass
    return reply


def capture_latest_media(ui, reader, job, b):
    latest = job["batch"][-1]
    message = latest["message"]
    kind=message.get('type',1)&65535
    if kind not in (3,47):
        return
    if kind==3 and reader.image_path(message.get('image_md5'),server_id=message.get('server_id')):
        return
    current = reader.messages(job["username"], limit=1, source=latest["source"])
    if not current or current[0]["local_id"] != message["local_id"]:
        return
    if (message.get("type",1)&65535)==47:
        # The worker can decode a cached original even when this bubble is no longer visible.
        hashes=message.get("sticker_hashes")
        if hashes:
            import sticker_media
            if sticker_media.original_bytes(reader.db_dir.parent,reader.account,hashes):return
    if not select_chat(ui, reader, job, b):
        return
    if kind==3:
        # Opening this exact conversation lets WeChat populate its on-demand photo cache.
        for _ in range(5):
            if reader.image_path(message.get('image_md5'),server_id=message.get('server_id')):
                return
            time.sleep(0.2)
    bubble = ui.latest_message()
    if not bubble or not b.is_image_stub(bubble[0]) or ui.incoming_state(bubble[0], bubble[1]) != "incoming":
        return
    paths = []
    try:
        frames = 3 if (message.get("type", 1) & 0xFFFF) == 47 else 1
        for index in range(frames):
            fd, path = tempfile.mkstemp(prefix="wechat-capture-", suffix=".png")
            os.close(fd)
            paths.append(path)
            if not ui.capture_region(b.image_capture_rect(ui, bubble[1], job["kind"] == "group"), path):
                raise RuntimeError("capture_failed")
            if index + 1 < frames:
                time.sleep(0.25)
        current = reader.messages(job["username"], limit=1, source=latest["source"])
        if current and current[0]["local_id"] == message["local_id"]:
            job.update(captures=paths, capture_event=latest["event_id"])
            return
    except Exception:
        pass
    for path in paths:
        os.unlink(path)


def migrate_memory(ui, reader, inbox, store, b, should_stop=lambda:False):
    if should_stop():return False
    sessions = reader.sessions(limit=10000)
    bounds = b.component_rect(ui.left_list, ui.pyatspi)
    pointer = ui.pointer()
    pointer.move(bounds.x + bounds.width // 2, bounds.y + bounds.height // 2)
    pointer.scroll(40, up=True)
    previous = None
    for _ in range(50):
        if should_stop():return False
        ui._discover()
        rows = ui.rows(ui.left_list)
        for name, _, row in rows:
            title = name.splitlines()[0].strip()
            matches = [s for s in sessions if s["title"] == title]
            if len(matches) == 1:
                old_key = b.session_key(str(getattr(row, "path", "")) + "\n" + title)
                if store.recent(old_key, 1):
                    inbox.memory_key(reader.account, matches[0]["username"], old_key)
        signature = tuple(name for name, _, _ in rows)
        if signature == previous:
            break
        previous = signature
        pointer.move(bounds.x + bounds.width // 2, bounds.y + bounds.height // 2)
        pointer.scroll(5)
    pointer.move(bounds.x + bounds.width // 2, bounds.y + bounds.height // 2)
    pointer.scroll(40, up=True)
    return not should_stop()


def run(cfg, config_path=None):
    import bridge as b
    from store import Store

    if not cfg["memory"]["enabled"]:
        raise RuntimeError("database_pipeline_requires_state_store")
    api_key = b.settings.read_api_key(cfg["api"]["key_file"])
    if not api_key:
        return 2
    factory = lambda: b.wechat_db_module.WeChatDB(keys_file=cfg["storage"]["wcdb_keys"], cache_dir=cfg["storage"]["db_cache"])
    reader = factory()
    store = Store(cfg["memory"]["db"], cfg["memory"]["retention_days"])
    from runtime_controls import RuntimeControls,is_command as owner_command
    controls=RuntimeControls(store,reader.account)
    base_cfg=copy.deepcopy(cfg)
    cfg=controls.apply(base_cfg)
    people = People(store)
    from sticker_library import StickerLibrary
    stickers=StickerLibrary(store,cfg['stickers']['library_dir'],reader.account) if cfg['stickers']['enabled'] else None
    store.recover_deliveries()
    for part in store._rows("SELECT * FROM outbox WHERE state='confirmed' AND event_id LIKE '%:part:%' ORDER BY created_at"):
        base = part["event_id"].rsplit(":part:",1)[0]
        parent = store._rows("SELECT delivery_id FROM outbox WHERE event_id=? AND session_key=?", (base,part["session_key"]))
        if parent:
            store.add_delivery_memory("user:" + parent[0]["delivery_id"], part["session_key"], "user", part["message"])
        store.add_delivery_memory("assistant:" + part["delivery_id"],part["session_key"],"assistant",part["reply"])
    inbox = Inbox(cfg["memory"]["db"])
    terminal = {r["event_id"] for r in store._rows("SELECT event_id FROM outbox WHERE state IN ('confirmed','unconfirmed')")}
    inbox.recover(terminal, reader.account)
    persona = {"text": b.settings.load_persona(cfg["persona_file"], b.agent.DEFAULT_PERSONA)}
    metrics = b.metrics_lib.Metrics(cfg["metrics"]["path"], cfg["metrics"]["enabled"])
    health=PipelineHealth(store,metrics)
    health.runtime(reader.account,'startup')
    tasks=Tasks(store) if cfg.get('tasks',{}).get('enabled') or cfg.get('followups',{}).get('enabled') else None
    if tasks:tasks.recover()
    from followups import Followups
    followups=Followups(store) if cfg.get('followups',{}).get('enabled') else None
    from feedback import Feedback
    feedback=Feedback(store) if cfg.get('learning',{}).get('enabled') else None
    semantic_selector=build_semantic_selector(cfg,api_key,store,b,metrics) if cfg['memory'].get('semantic_enabled') else None
    responder = b.build_responder(cfg, store, api_key, persona, metrics)
    vision_key = b.settings.read_api_key(cfg["vision"].get("key_file") or cfg["api"]["key_file"])
    vision_fn = (lambda paths, prompt: b.vision.describe(cfg["vision"], paths, vision_key, prompt=prompt)) if cfg["vision"]["enabled"] and vision_key else None
    from sticker_collect import CollectQueue
    collection_queue=CollectQueue(store,reader.account) if stickers and vision_fn else None
    def learn_person(reader,scene,messages,source):
        messages=normalize_forget_messages(messages,b,cfg)
        people.record(reader.account,scene['username'],scene['title'],messages,source,reader.contacts())
        if feedback:
            for message in messages:
                person=message.get('sender','') if scene['username'].endswith('@chatroom') else scene['username']
                forgotten=forget_request(own_text(message)) if not message.get('outgoing') and not message.get('quote_sender') and (message.get('type',1)&65535)==1 else ''
                if person and forgotten:feedback.erase(reader.account,person,message.get('timestamp',time.time()),kind=forgotten)
        if collection_queue and cfg['stickers'].get('collect_enabled',True) and cfg['stickers'].get('passive_collect'):
            for message in messages:collection_queue.enqueue(scene['username'],dict(source=source,message=message))

    ingestion = Reader(factory, inbox, cfg, lambda text: b.mentioned_self(text, b.policy_self_names(cfg)),on_messages=learn_person)
    ingestion.account = reader.account
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True
        ingestion.stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker_count = max(2, int(cfg["dispatch"]["workers"]))
    pool = ThreadPoolExecutor(max_workers=worker_count)
    futures, ready = {}, []
    sticker_future=None
    favorite_attempts={}
    last_sticker_maintenance=0
    ui = None
    engaged, topics, interjected, interject_checked = {}, {}, {}, {}
    heartbeat = last_moments = last_interactions = time.monotonic()
    last_interactions=0
    last_background_attempt=0
    last_task_attempt=0
    last_purge = 0
    config_file = config_path or os.path.join(os.path.dirname(__file__), "config.json")
    config_mtime = b.file_mtime(config_file)
    last_reload = 0
    last_session_check = 0
    session = None
    legacy_migrated = False
    bound_pid = None
    owners = {}
    active_job = None
    active_batch = None
    # Freeze existing owner titles to stable IDs once, then persist across renames/restarts.
    with inbox.lock, inbox.db:
        inbox.db.execute("CREATE TABLE IF NOT EXISTS pipeline_owner(account TEXT,username TEXT,label TEXT,PRIMARY KEY(account,username))")
        owners = {r[0]: r[1] for r in inbox.db.execute("SELECT username,label FROM pipeline_owner WHERE account=?", (reader.account,))}
        if cfg["relay"]["allowed_titles"]:
            sessions = reader.sessions(limit=10000)
            for title in cfg["relay"]["allowed_titles"]:
                if title.casefold() in owners.values():
                    continue
                matches = [s for s in sessions if s["title"].casefold() == title.casefold()]
                if len(matches) == 1:
                    owners[matches[0]["username"]] = title.casefold()
            inbox.db.executemany("INSERT OR IGNORE INTO pipeline_owner VALUES(?,?,?)", [(reader.account, user, label) for user, label in owners.items()])
    LOG.info("database_pipeline_ready pending=%d workers=%d", inbox.count(reader.account), worker_count)

    def complete(job, result, wait=0):
        nonlocal active_job, active_batch
        inbox.complete(job["batch"], result, wait=wait)
        reason=job.get('skip_reason')
        stage='skipped' if reason and result=='done' else 'confirmed' if result=='done' else 'uncertain' if result=='unconfirmed' else 'deferred' if result=='defer' else 'retry'
        health.note(job,stage,reason or ('sent' if result=='done' else 'send_unknown' if result=='unconfirmed' else 'waiting' if result=='defer' else 'send_retry'))
        if active_job is job:
            active_job = None
        if active_batch is job["batch"]:
            active_batch = None
        if result not in ("retry", "defer"):
            for path in [job.get("wav"), job.get("capture")] + job.get("captures", []):
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    try:
        while not stopped:
            try:
                now = time.monotonic()
                if now - last_reload >= 10:
                    last_reload = now
                    if b.file_mtime(config_file) != config_mtime:
                        fresh = b.settings.load(config_file)
                        # Paths, account and transport settings require an explicit service restart.
                        for key in ("policy", "proactive", "moments", "delivery", "voice", "quality", "relay"):
                            base_cfg[key] = fresh[key]
                        persona["text"] = b.settings.load_persona(fresh["persona_file"], b.agent.DEFAULT_PERSONA)
                        config_mtime = b.file_mtime(config_file)
                        LOG.info("db_config_reloaded")
                cfg=controls.apply(base_cfg)
                ingestion.cfg=cfg
                if now - last_purge >= 3600:
                    store.purge()
                    health.purge()
                    if feedback:feedback.purge(cfg['memory']['retention_days'])
                    if followups:followups.purge()
                    inbox.purge(cfg["memory"]["retention_days"])
                    people.purge(cfg['memory']['retention_days'])
                    last_purge = now
                if now - last_session_check >= 2 or session is None:
                    last_session_check = now
                    session = b.wechat_session()
                    if session and not active_account(session[0], reader):
                        health.runtime(reader.account,'account_mismatch')
                        raise RuntimeError("active_account_not_verified")
                    health.runtime(reader.account,'offline' if not session else 'reader_error' if ingestion.errors else 'online')
                if not session:
                    time.sleep(1)
                    continue
                if session[0] != bound_pid:
                    if not active_account(session[0], reader):
                        raise RuntimeError("active_account_not_verified")
                    bound_pid = session[0]
                    ui = None
                    if not ingestion.is_alive():
                        ingestion.start()
                b.bind_session(session)
                if ui is None:
                    ui = b.WeChatUI()
                if not legacy_migrated:
                    from startup_memory import migrate_once
                    migrated=migrate_once(store,reader.account,lambda:migrate_memory(ui,reader,inbox,store,b,lambda:stopped))
                    if stopped:continue
                    legacy_migrated = True
                    LOG.info("db_executor_ready account_verified=true legacy_scan=%s",migrated)
                if sticker_future and sticker_future[0].done():
                    future,collection_job=sticker_future;sticker_future=None
                    try:collection_result=future.result()
                    except Exception as exc:
                        LOG.warning('db_passive_sticker_failed=%s',type(exc).__name__);collection_result='retry'
                    collection_queue.finish(collection_job,collection_result)
                    LOG.info('db_passive_sticker result=%s',collection_result)
                for key, (future, job) in list(futures.items()):
                    if future.done():
                        del futures[key]
                        try:
                            reply = future.result()
                            if reply==STICKER_MEDIA_READY:
                                job['favorites_attempted']=True
                                try:
                                    if cfg['stickers'].get('collect_enabled',True):sync_sticker(ui,reader,job,stickers,store,b,should_yield=lambda:sticker_should_yield(inbox,job))
                                except Exception as exc:LOG.warning('db_sticker_prepare_failed=%s',type(exc).__name__)
                                futures[key]=(pool.submit(prepare_model,job,factory(),responder,vision_fn,b,copy.deepcopy(cfg),people,stickers,health,feedback,semantic_selector),job)
                                continue
                            inbox.save_reply(job["batch"], freeze_reply(job,reply))
                            ready.append((job, reply))
                        except Exception as exc:
                            LOG.warning("db_model_failed=%s event=%s", type(exc).__name__, job["event_id"])
                            job['skip_reason']='model_failed'
                            complete(job, "retry")
                ready.sort(key=lambda pair: (-max(r["priority"] for r in pair[0]["batch"]), -pair[0]["batch"][-1]["ts"]))
                if ready:
                    job, reply = ready.pop(0)
                    active_job = job
                    if job['kind']=='group' and cfg['policy'].get('group_strict_mention') and not job['is_mention']:
                        job['skip_reason']='policy';store.mark_event(job['event_id']);complete(job,'done');continue
                    job["text_started"] = bool(store._rows("SELECT 1 FROM outbox WHERE session_key=? AND event_id LIKE ? LIMIT 1", (job["session_key"],job["event_id"] + ':part:%')))
                    obsolete=obsolete_reply(reader,job,inbox,store,b,cfg)
                    if obsolete=='wait':
                        complete(job,'defer',1);continue
                    if obsolete=='obsolete':
                        store.add_delivery_memory('obsolete:'+job['event_id'],job['session_key'],'user',job['message'])
                        job['skip_reason']='obsolete';store.mark_event(job['event_id']);complete(job,'done');continue
                    stale=stale_group_reply(reader,job,b,cfg) or not semantic_reply_current(job,people)
                    if stale or b.is_silent_reply(reply) or (job.get("group_reply_mode") in ("proactive", "interject") and b.proactive_pass(reply)):
                        job['skip_reason']='stale_turn' if stale else 'model_silent'
                        store.add_message(job["session_key"], "user", job["message"])
                        store.mark_event(job["event_id"])
                        complete(job, "done")
                    else:
                        wait = b.quota_wait(store, job, cfg)
                        if wait:
                            job['skip_reason']='quota'
                            complete(job, "defer", wait)
                        else:
                            health.note(job,'sending','waiting')
                            if stickers and cfg['stickers'].get('collect_enabled',True):
                                try:sync_sticker(ui,reader,job,stickers,store,b,should_yield=lambda:sticker_should_yield(inbox,job))
                                except Exception as exc:LOG.warning('db_sticker_sync_failed=%s',type(exc).__name__)
                            if job.get('sticker_only'):
                                try:result=deliver_sticker_only(ui,reader,job,reply,stickers,store,b,cfg,should_yield=lambda:sticker_should_yield(inbox,job))
                                except Exception as exc:
                                    LOG.warning('db_sticker_only_failed=%s',type(exc).__name__)
                                    result='unconfirmed'
                                if result=='done' and not job.get('skip_reason'):remember_group_reply(job,'[表情]',cfg['policy'],b,engaged,topics,interjected,time.monotonic())
                                complete(job,result)
                                continue
                            if stickers and sticker_requested(job):
                                try:sticker_result=send_sticker(ui,reader,job,reply,stickers,store,b,cfg,explicit=True,should_yield=lambda:sticker_should_yield(inbox,job))
                                except Exception as exc:
                                    LOG.warning('db_sticker_send_failed=%s',type(exc).__name__);sticker_result='skipped'
                                if sticker_result in ('confirmed','unconfirmed'):
                                    if sticker_result=='confirmed':remember_group_reply(job,'[表情]',cfg['policy'],b,engaged,topics,interjected,time.monotonic())
                                    store.mark_event(job['event_id'])
                                    complete(job,'done' if sticker_result=='confirmed' else 'unconfirmed')
                                    continue
                            result = send_reply(ui, reader, job, reply, store, b, cfg)
                            if result=='done':remember_group_reply(job,reply,cfg['policy'],b,engaged,topics,interjected,time.monotonic())
                            if result=='done' and stickers:
                                try:send_sticker(ui,reader,job,reply,stickers,store,b,cfg,should_yield=lambda:sticker_should_yield(inbox,job))
                                except Exception as exc:LOG.warning('db_sticker_send_failed=%s',type(exc).__name__)
                            complete(job, result, job.get("defer_seconds", 0))
                busy = set(futures) | {j["username"] for j, _ in ready}
                sticker_work=int(sticker_future is not None)
                capacity = worker_count - len(futures) - len(ready)-sticker_work
                for _ in range(max(0, capacity)):
                    pacing={}
                    if cfg['delivery'].get('coalesce_seconds',0)>0:
                        pacing=dict(quiet_seconds=cfg['delivery']['coalesce_seconds'],max_wait_seconds=cfg['delivery'].get('max_coalesce_seconds',4),bypass_wait=lambda message:owner_command(own_text(message)))
                    batch = inbox.claim(busy, account=reader.account,**pacing)
                    if not batch:
                        break
                    active_batch = batch
                    job = build_job(batch, inbox, b, cfg, reader.account)
                    active_job = job
                    health.note(job,'queued','claimed')
                    focus_stamp=job['focus']['message'].get('timestamp') or time.time()
                    job['conversation_context']=people.context(reader.account,job['person_id'],job['username'],job['focus_text'],
                        exclude=[own_text(row['message']) for row in batch],now=focus_stamp)
                    job['suppress_legacy']=people.suppress_legacy(reader.account,job['person_id']) or bool(cfg['memory'].get('scene_state_enabled'))
                    if job['kind']=='private' and cfg['memory'].get('scene_state_enabled'):
                        job['scene_history']=people.safe_assistant_history(reader.account,job['person_id'],job['username'],job['session_key'],now=focus_stamp)
                    memory_text=own_text(normalize_forget_messages([job['focus']['message']],b,cfg)[0])
                    job['memory_response']=people.memory_reply(memory_text,account=reader.account,person=job['person_id'],stamp=job['focus']['message'].get('timestamp',0))
                    if cfg['memory'].get('scene_state_enabled'):
                        job['visual_context']=job['conversation_context']+'\n'+'\n'.join('小i：'+text[:300] for _,text in job.get('scene_history',[]))
                    else:
                        job["visual_context"] = "\n".join(("对方：" if role == "user" else "小i：") + text[:300] for role, text in store.recent(job["session_key"], 5))
                    if job["kind"] == "group":
                        job["sender_names"] = reader.contacts()
                        sender_id = job['person_id']
                        job["sender"] = job["sender_names"].get(sender_id, "")
                        job['group_context']=group_transcript(reader,job,job['sender_names'],people=people)
                        job['group_focus']=group_focus(job,reader.account,job['sender_names'])
                        if cfg['memory'].get('scene_state_enabled'):
                            job['group_focus']+='\n本群有来源的话题记忆：\n'+people.topic_context(reader.account,job['username'],job['focus_text'],now=focus_stamp)[:2000]
                        job['target_label']=speaker_label(reader.account,sender_id,job['sender_names'])
                        job['visual_context']=job['group_context']
                    busy.add(job["username"])
                    if store.seen_event(job["event_id"]):
                        job['skip_reason']='duplicate'
                        complete(job, "done")
                        continue
                    if all((r["message"].get("type", 1) & 0xFFFF) not in (1, 3, 34, 47) for r in batch):
                        job['skip_reason']='unsupported'
                        complete(job, "done")
                        continue
                    is_group = job["kind"] == "group"
                    active = False
                    interject = False
                    if is_group:
                        active,eligible=group_turn(job,reader.account,cfg['policy'],b,engaged,topics,now)
                        interject = eligible and b.should_interject(job, cfg, interjected, now,checked_map=interject_checked)
                        if not group_reply_relevant(job,active,eligible,interject,cfg):
                            job['skip_reason']='policy';store.mark_event(job['event_id']);complete(job,'done');continue
                    job["force_reply"] = bool(is_group and job["is_mention"])
                    job["group_reply_mode"] = "mention" if job["is_mention"] else "engaged" if active else "interject" if interject else "private" if not is_group else "skip"
                    ok, why = b.policy.allowed(cfg["policy"], dict(name=job["title"], kind=job["kind"], is_mention=job["is_mention"], engaged=active, interject=interject, group_reply_mode=job["group_reply_mode"]))
                    cap = cfg["policy"]["daily_reply_cap"]
                    if not ok or (cap and store.usage_today()["calls"] >= cap):
                        job['skip_reason']='policy' if not ok else 'daily_cap'
                        if is_group:
                            store.add_message(job["session_key"], "user", job["message"])
                        store.mark_event(job["event_id"])
                        complete(job, "done")
                        continue
                    if is_group and not job["is_mention"] and len(futures) + len(ready)+sticker_work >= worker_count - 1:
                        job['skip_reason']='group_capacity'
                        complete(job, "defer", 1)
                        break
                    job['self_names']=b.policy_self_names(cfg)
                    owner=verified_management_owner(job,owners,cfg)
                    command_reply=None
                    if not batch[0].get('reply'):
                        command_reply=capability_reply(job,reader,store,cfg,controls,owner,stickers,followups)
                        if job.get('sticker_continue_requested') and cfg['stickers'].get('collect_enabled',True):
                            command_reply=continue_sticker_request(ui,reader,job,stickers,store,b,lambda:sticker_should_yield(inbox,job))
                        if tasks and command_reply is None:command_reply=task_command_reply(tasks,job)
                        if command_reply is None and not job.get('quote_sender') and job['focus_text'].strip() in ('消息处理状态','运行状态','为什么没回复','为什么不回消息','不回复原因'):
                            command_reply=health.report(job)
                    if command_reply is not None:
                        job['command_response']=True
                        command_reply=command_reply[:1200]
                        inbox.save_reply(batch,freeze_reply(job,command_reply))
                        ready.append((job,command_reply))
                        health.note(job,'ready','command')
                        active_job=active_batch=None
                        continue
                    choose_command(job, b, cfg, owners.get(job['username']) in {t.casefold() for t in cfg['relay']['allowed_titles']})
                    if job["command"]:
                        if not select_chat(ui, reader, job, b):
                            complete(job, "retry")
                            continue
                        reply=execute_command(ui,reader,job,store,b,cfg)
                        inbox.save_reply(batch, reply)
                        ready.append((job, reply))
                        active_job = active_batch = None
                    else:
                        if not batch[0].get("reply"):
                            try:
                                capture_latest_media(ui, reader, job, b)
                            except Exception as exc:
                                LOG.warning("db_capture_failed=%s", type(exc).__name__)
                        if job.get('group_reply_mode')=='interject':interject_checked[job['identity']]=time.monotonic()
                        futures[job["username"]] = (pool.submit(prepare_model, job, factory(), responder, vision_fn, b, copy.deepcopy(cfg), people, stickers,health,feedback,semantic_selector), job)
                        active_job = active_batch = None
                if collection_queue and cfg['stickers'].get('collect_enabled',True) and cfg['stickers'].get('passive_collect') and sticker_future is None and worker_count-len(futures)-len(ready)>=2 and not inbox.count(reader.account,min_priority=3):
                    collection_job=collection_queue.claim(set(futures)|{j['username'] for j,_ in ready})
                    if collection_job:sticker_future=(pool.submit(collect_queued_sticker,collection_job,factory(),stickers,vision_fn),collection_job)
                urgent_busy=any(j['kind']=='private' or j.get('is_mention') for _,j in futures.values()) or any(j['kind']=='private' or j.get('is_mention') for j,_ in ready)
                if tasks and not urgent_busy and now-last_task_attempt>=5 and time.monotonic()-ingestion.last_ok<5:
                    last_task_attempt=now
                    run_due_task(ui,reader,tasks,store,inbox,b,cfg,health=health,followups=followups)
                if stickers and cfg['stickers'].get('passive_collect') and not futures and not ready and not sticker_future and not inbox.count(reader.account) and now-last_sticker_maintenance>=15:
                    last_sticker_maintenance=now
                    try:maintain_native_stickers(ui,reader,stickers,store,b,favorite_attempts,now,lambda:stopped or bool(inbox.count(reader.account,min_priority=3)))
                    except Exception as exc:LOG.warning('db_sticker_maintenance_failed=%s',type(exc).__name__)
                interactions_due=last_interactions+cfg['moments'].get('interaction_minutes',10)*60 if last_interactions else 0
                scan_due=last_moments+cfg['moments']['scan_minutes']*60
                interaction_enabled=cfg['moments']['enabled'] and cfg['moments'].get('interaction_enabled') and cfg['moments'].get('interaction_minutes',0)>0
                which='interactions' if interaction_enabled and now>=interactions_due else 'scan'
                due=interactions_due if which=='interactions' else scan_due
                busy=bool(futures or ready or sticker_future or inbox.count(reader.account))
                if cfg['moments']['enabled'] and now-last_background_attempt>=30 and time.monotonic()-ingestion.last_ok<5 and background_allowed(inbox,reader.account,due,now,busy):
                    last_background_attempt=now
                    deadline=time.monotonic()+20
                    fair=busy
                    should_yield=lambda: stopped or time.monotonic()>=deadline or bool(inbox.count(reader.account,min_priority=3 if fair else 0))
                    client=b.agent.ModelClient(cfg['api']['url'],cfg['api']['model'],api_key,timeout=cfg['api']['timeout'],max_tokens=400)
                    if which=='interactions':
                        replier=interruptible_model(pool,b.moments_agent.make_replier(client,persona['text']),should_yield)
                        result=b.run_interactions_pass(ui,cfg,store,metrics,replier,should_yield=should_yield,max_actions=1)
                        last_interactions=now
                        if result['replied'] and result.get('remaining',0)>0:
                            last_interactions=now-cfg['moments']['interaction_minutes']*60+30
                    else:
                        commenter=interruptible_model(pool,b.moments_agent.make_commenter(client,persona['text']),should_yield)
                        b.run_moments_pass(ui,cfg,store,metrics,commenter,should_yield=should_yield,max_actions=1)
                        last_moments=now
                    LOG.info('db_background_slot kind=%s fair=%s',which,fair)
                    ui._discover()
                if not futures and not ready and not inbox.count(reader.account) and time.monotonic() - ingestion.last_ok < 5:
                    if cfg["proactive"]["enabled"]:
                        for greeting_key, entry in b.extras.due_greetings(cfg["proactive"]["greetings"], time.time(), set()):
                            event = "greeting:" + b.digest(greeting_key)[:24]
                            if store.seen_event(event):
                                continue
                            targets = [s for s in reader.sessions(limit=10000) if s["title"] == entry["contact"]]
                            if len(targets) != 1:
                                continue
                            target = targets[0]
                            task = dict(username=target["username"], title=target["title"], account=reader.account,
                                        session_key=inbox.memory_key(reader.account, target["username"]), event_id=event,
                                        message="[已配置的定时问候]", kind="group" if target["username"].endswith("@chatroom") else "private", sender="")
                            send_reply(ui, reader, task, entry["text"], store, b, cfg)
                            if inbox.count(reader.account):
                                break
                if now - heartbeat >= 60:
                    heartbeat = now
                    LOG.info("db_pipeline_heartbeat inbox=%d models=%d ready=%d reader_age=%ds", inbox.count(), len(futures), len(ready), int(time.monotonic() - ingestion.last_ok) if ingestion.last_ok else -1)
            except Exception as exc:
                recover_abandoned(inbox, store, active_job, active_batch)
                active_job = active_batch = None
                LOG.warning("db_executor_error=%s", type(exc).__name__)
                ui = None
                time.sleep(1)
            time.sleep(0.2)
    finally:
        ingestion.stop_event.set()
        if ingestion.ident is not None:
            ingestion.join(timeout=25)
        pool.shutdown(wait=True, cancel_futures=True)
        if not ingestion.is_alive():
            inbox.close()
            store.close()
        LOG.info("database_pipeline_stopping")
    return 0
