"""Own-post comment IDs drive replies; only a new native comment confirms delivery."""
from datetime import datetime
import hashlib
import logging
import os
import re
import tempfile
import time
from zoneinfo import ZoneInfo
import moments
import moments_feed
import ocr as ocr_lib

LOG=logging.getLogger('wechat-autoreply')
RELATIVE=re.compile(r'^(.*?)\s+\d+\s+(?:second|minute|hour|day)\(s\)\s+ago(?:\s+(.*))?$')


def norm(value):return re.sub(r'\W+','',value).casefold()


def start_time(store,account,now=None):
    now=time.time() if now is None else now
    midnight=datetime.fromtimestamp(now,ZoneInfo('Asia/Shanghai')).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    store._write('CREATE TABLE IF NOT EXISTS moment_reply_start(account TEXT PRIMARY KEY,ts REAL NOT NULL)')
    store._write('INSERT OR IGNORE INTO moment_reply_start VALUES(?,?)',(account,midnight))
    return store._rows('SELECT ts FROM moment_reply_start WHERE account=?',(account,))[0]['ts']


def pending(posts,account,store,since):
    result=[]
    for post in posts:
        if post['username']!=account:continue
        answered={c['ref_id'] for c in post.get('thread',[]) if c['username']==account and not c['deleted']}
        for c in post.get('thread',[]):
            if not c['id'].isdecimal() or int(c['id'])==0 or c['username']==account or c['deleted'] or c['timestamp']<since or c['id'] in answered:continue
            event='moment-reply:'+hashlib.sha256('\0'.join((account,post['id'],c['username'],c['id'])).encode()).hexdigest()[:24]
            delivery=store.delivery(event)
            if delivery and delivery['state'] in ('sending','unconfirmed','confirmed'):continue
            result.append(dict(post=post,comment=c,event=event))
    return sorted(result,key=lambda target:target['comment']['timestamp'])


def delivered(posts,account,post,comment,text,before):
    for current in posts:
        if current['id']!=post['id'] or current['username']!=account:continue
        return any(c['username']==account and not c['deleted'] and c['id'] not in before and
                   (c['ref_id']==comment['id'] or norm(c['text'])==norm(text)) for c in current.get('thread',[]))
    return False


def shown(ui,obj):
    box=moments._rect(obj,ui.pyatspi)
    return obj.getState().contains(ui.pyatspi.STATE_SHOWING) and box.width>0 and box.height>0


def details_match(ui,b,post):
    frame=b.details_frame(ui)
    if frame is None:return False
    expected=norm(post['text'])
    return bool(expected) and any(obj.getRoleName()=='list item' and shown(ui,obj) and expected in norm(obj.name or '') for obj in moments.walk(frame,5000))


def open_details(ui,b,feed,post,comment,aliases,should_yield):
    if details_match(ui,b,post):return feed.poster.activate_feed()
    for attempt in range(4):
        if should_yield() or not feed.open():return False
        panel,button=b._open_interaction_panel(feed,ui)
        if panel is None:return False
        candidates=[]
        for obj in moments.walk(panel,8000):
            try:
                if obj.getRoleName()!='list item' or not shown(ui,obj):continue
                match=RELATIVE.match((obj.name or '').strip())
                if not match or norm(match.group(1)) not in {norm(a) for a in aliases if a}:continue
                body=match.group(2) or ''
                score=2 if comment['text'] and norm(comment['text']) in norm(body) else 1 if not body else 0
                candidates.append((score,obj))
            except Exception:continue
        candidates.sort(key=lambda pair:pair[0],reverse=True)
        if attempt>=len(candidates):b._close_interaction_panel(ui,button);return False
        box=feed.poster.rect(candidates[attempt][1])
        ui.pointer().tap(box.x+box.width//2,box.y+box.height//2);time.sleep(1)
        if details_match(ui,b,post):return feed.poster.activate_feed()
        feed.close()
    return False


def words(ui,region,ocr):
    fd,path=tempfile.mkstemp(prefix='own-moment-ocr-',suffix='.png');os.close(fd)
    try:
        if not ui.capture_region(region,path):return []
        return ocr.words(path)
    finally:os.unlink(path)


def comment_region(ui,frame,poster):
    frame_box=poster.rect(frame)
    regions=[]
    for obj in moments.walk(frame,5000):
        try:
            if obj.getRoleName()!='list item' or (obj.name or '').strip() or not shown(ui,obj):continue
            box=poster.rect(obj)
            top=max(box.y,frame_box.y+50);bottom=min(box.y+box.height,frame_box.y+frame_box.height-1)
            if box.width>frame_box.width*.8 and bottom-top>20:regions.append((box.x,top,box.width,bottom-top))
        except Exception:continue
    return regions[0] if len(regions)==1 else None


def comment_points(region,name,body):
    if body:
        x,y,w,h=body[0]
        return [(region[0]+x+w//2,region[1]+y+h//2)]
    x,y,w,h=name
    points=[(x+w+12,y+h//2),(x+10,y+h+12),(region[2]-35,y+h//2)]
    return [(region[0]+min(region[2]-2,max(2,px)),region[1]+min(region[3]-2,max(2,py))) for px,py in points]


def locate_comment(region,observed,text,aliases):
    # Referenced names in reply bodies sit farther right than the author column.
    names=[(x,y,w,h) for word,x,y,w,h in observed if norm(word) in aliases and x<region[2]*.45]
    candidates=[]
    for name in names:
        nx,ny,nw,nh=name
        body=[(x,y,w,h) for word,x,y,w,h in observed if len(norm(word))>=2 and norm(word) in norm(text) and norm(word) not in aliases and ny-8<=y<=ny+65]
        if body:candidates.append((name,body))
    if len(candidates)==1:return candidates[0]
    if len(names)==1:return names[0],[]
    # shortcut: indistinguishable repeated emoji rows are skipped; use native row IDs for ambiguous threads.
    return None


def reply_to_comment(ui,b,feed,comment,aliases,text,ocr,before_send,should_yield):
    frame=b.details_frame(ui)
    if frame is None or not feed.poster.activate_feed():return 'details_missing'
    box=feed.poster.rect(frame);fbox=(box.x,box.y,box.width,box.height)
    ui.pointer().move(box.x+box.width//2,box.y+box.height//2);ui.pointer().scroll(15,up=False);time.sleep(.5)
    aliases={norm(a) for a in aliases if len(norm(a))>=2}
    points=[]
    for page in range(5):
        if should_yield():return 'yield'
        region=comment_region(ui,frame,feed.poster)
        if region:
            observed=words(ui,region,ocr)
            found=locate_comment(region,observed,comment['text'],aliases)
            if found:
                points=comment_points(region,*found);break
        ui.pointer().scroll(4,up=True);time.sleep(.4)
    if not points:return 'target_not_found'
    editor=None
    for point in points:
        if should_yield():return 'yield'
        if not feed.poster.activate_feed():return 'details_not_focused'
        ui.pointer().tap(*point);time.sleep(.7)
        editor=feed._editor_box(fbox,(0,box.y+box.height,0,0))
        if editor is not None:break
    if editor is None:return 'editor_missing'
    prompt=' '.join(word for word,*_ in words(ui,(editor[0]+5,editor[1]+5,editor[2]-10,min(45,editor[3]-10)),ocr))
    if not ('reply' in prompt.casefold() or '回复' in prompt) or not any(alias in norm(prompt) for alias in aliases):return 'reply_target_unverified'
    checked=moments_feed.MomentsFeed(ui,pointer=ui.pointer(),ocr_reader=ocr)
    typed=checked.enter_comment(text,fbox,(0,box.y+box.height,0,0),placeholder=prompt)
    if typed!='typed':
        observed=' '.join(word for word,*_ in words(ui,(editor[0]+5,editor[1]+5,editor[2]-10,max(10,editor[3]-40)),ocr))
        from difflib import SequenceMatcher
        a=norm(text);v=norm(observed);match=SequenceMatcher(None,a,v).find_longest_match()
        LOG.warning('db_moment_draft_check status=%s expected_chars=%d observed_chars=%d normalized=%s common_span=%s editor=%s',typed,len(text),len(observed),str((len(a),len(v))),str((match.a,match.b,match.size)),str(editor))
        return 'not_typed'
    editor=feed._editor_box(fbox,(0,box.y+box.height,0,0))
    if editor is None:return 'editor_missing_after_prepare'
    if should_yield() or not before_send():return 'yield'
    ui.pointer().tap(editor[0]+editor[2]-60,editor[1]+editor[3]-21)
    return 'submitted'


def run(ui,cfg,store,metrics,replier,b,reader=None,should_yield=None,dry_run=False,max_actions=None):
    reader=reader or b.get_wechat_db(cfg)
    should_yield=should_yield or (lambda:False)
    report=dict(scanned=0,replied=[],skipped=[],seeded=0,dry_run=dry_run,remaining=0)
    if reader is None or store is None:return report
    targets=pending(reader.moments(limit=100),reader.account,store,start_time(store,reader.account))
    report['remaining']=len(targets)
    names=reader.contacts();feed=moments_feed.MomentsFeed(ui,pointer=ui.pointer())
    ocr=ocr_lib.Ocr(lang=cfg['ocr']['lang'],timeout=cfg['ocr']['timeout'],enabled=cfg['ocr']['enabled'])
    used_ui=False
    attempts=0
    try:
        for target in targets:
            if max_actions is not None and attempts>=max_actions:break
            if should_yield():break
            event=target['event'];post=target['post'];c=target['comment'];report['scanned']+=1
            aliases=[c['nickname'],names.get(c['username'],'')]
            if dry_run:report['skipped'].append([event,'dry_run']);continue
            existing=store.delivery(event)
            text=existing['reply'] if existing else b.moments_agent.clean_comment(replier(aliases[-1] or aliases[0],c['text'],post['text']) if replier else '')
            if not text:report['skipped'].append([event,'empty_reply']);continue
            store.start_delivery(event,'moment:'+reader.account+':'+post['id'],event,c['text'],text)
            used_ui=True
            if not open_details(ui,b,feed,post,c,aliases,should_yield):report['skipped'].append([event,'details_unverified']);continue
            before={row['id'] for row in next((p for p in reader.moments(limit=100) if p['id']==post['id']),post).get('thread',[]) if row['username']==reader.account}
            def before_send():
                if should_yield():return False
                store.set_delivery_state(event,'sending');return True
            result=reply_to_comment(ui,b,feed,c,aliases,text,ocr,before_send,should_yield)
            if result=='submitted' or store.delivery(event)['state']=='sending':attempts+=1
            if result!='submitted':
                if store.delivery(event)['state']=='sending':store.set_delivery_state(event,'unconfirmed')
                report['skipped'].append([event,result]);continue
            confirmed=False
            for _ in range(10):
                time.sleep(1)
                if delivered(reader.moments(limit=100),reader.account,post,c,text,before):confirmed=True;break
            store.set_delivery_state(event,'confirmed' if confirmed else 'unconfirmed')
            report['remaining']-=1
            (report['replied'] if confirmed else report['skipped']).append(event if confirmed else [event,'unconfirmed'])
            LOG.info('db_moment_reply event=%s result=%s',event,'confirmed' if confirmed else 'unconfirmed')
    finally:
        if used_ui and not feed.poster.compose_open():
            if not feed.close():feed.close()
    metrics.event('interactions_pass',scanned=report['scanned'],replied=len(report['replied']),skipped=len(report['skipped']),seeded=0,dry_run=dry_run)
    LOG.info('interactions_pass scanned=%d replied=%d skipped=%d seeded=0 dry_run=%s',report['scanned'],len(report['replied']),len(report['skipped']),dry_run)
    for event,reason in report['skipped']:LOG.info('db_moment_reply_skip event=%s reason=%s',event,reason)
    return report
