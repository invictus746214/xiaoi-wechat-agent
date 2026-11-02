"""Scene-scoped message diagnostics containing hashes and fixed reason codes only."""
import hashlib,json,math,time

STAGES={'queued','media','model','ready','sending','confirmed','uncertain','deferred','retry','skipped','failed'}
REASONS={'claimed':'已入队','generating':'生成回复','media_read':'读取媒体','media_unreadable':'媒体未能读取','model_silent':'主动沉默','model_failed':'模型失败','policy':'策略跳过','quota':'频率限制','stale_turn':'已被新发言替代','unsupported':'不支持的消息','duplicate':'重复事件','sent':'确认送达','send_unknown':'送达未确认','send_retry':'发送前失败','waiting':'等待处理','command':'处理指令','sticker_unavailable':'表情暂不可用','daily_cap':'达到日限','group_capacity':'保留私聊通道'}
REASONS.update(obsolete='旧回复已撤回',requeued='新发言已重新排队',voice_timeout='语音识别超时',
               voice_unavailable='语音识别暂不可用',offline='微信离线',reader_error='消息读取异常',
               account_mismatch='微信账号不匹配',startup='正在启动')
PHASES={'native_read':'读取原语音','available_probe':'检查识别环境','imports':'加载识别依赖',
        'decode':'语音解码','model':'加载语音模型','recognition':'语音识别','worker':'识别子进程','total':'语音处理总耗时'}
RUNTIME={'online':'最近检查在线','offline':'微信离线','account_mismatch':'微信账号不匹配',
         'reader_error':'消息读取异常','startup':'正在启动'}

class PipelineHealth:
    def __init__(self,store,metrics=None):
        self.store=store;self.metrics=metrics
        store._write('CREATE TABLE IF NOT EXISTS pipeline_trace(event_hash TEXT PRIMARY KEY,scene_hash TEXT,kind TEXT,stage TEXT,reason TEXT,updated REAL,history TEXT)')
        store._write('CREATE INDEX IF NOT EXISTS pipeline_trace_scene ON pipeline_trace(scene_hash,updated)')
        store._write('CREATE TABLE IF NOT EXISTS pipeline_phase(event_hash TEXT,scene_hash TEXT,phase TEXT,seconds REAL,updated REAL,PRIMARY KEY(event_hash,phase))')
        store._write('CREATE INDEX IF NOT EXISTS pipeline_phase_scene ON pipeline_phase(scene_hash,updated)')
        store._write('CREATE INDEX IF NOT EXISTS pipeline_phase_updated ON pipeline_phase(updated)')
        store._write('CREATE TABLE IF NOT EXISTS pipeline_runtime(account_hash TEXT PRIMARY KEY,status TEXT,updated REAL)')

    @staticmethod
    def scene(job):
        return hashlib.sha256((str(job.get('account',''))+'\0'+str(job.get('username',''))).encode()).hexdigest()

    def note(self,job,stage,reason='waiting',duration_ms=None,now=None):
        try:
            if stage not in STAGES or reason not in REASONS:return False
            stamp=time.time() if now is None else now
            event=hashlib.sha256(str(job['event_id']).encode()).hexdigest()
            item=dict(stage=stage,reason=reason,at=stamp)
            if duration_ms is not None:item['duration_ms']=max(0,min(int(duration_ms),86400000))
            kind='group' if job.get('kind')=='group' else 'private'
            with self.store._lock,self.store._conn:
                old=self.store._conn.execute('SELECT history FROM pipeline_trace WHERE event_hash=?',(event,)).fetchone()
                history=json.loads(old[0]) if old else []
                history=(history+[item])[-16:]
                self.store._conn.execute('INSERT INTO pipeline_trace VALUES(?,?,?,?,?,?,?) ON CONFLICT(event_hash) DO UPDATE SET stage=excluded.stage,reason=excluded.reason,updated=excluded.updated,history=excluded.history',
                    (event,self.scene(job),kind,stage,reason,stamp,json.dumps(history)))
            if self.metrics:
                self.metrics.event('pipeline_stage',event_hash=event,scene_hash=self.scene(job),kind=kind,**item)
            return True
        except Exception:return False

    def note_phase(self,job,name,seconds,now=None):
        """Keep the first fixed-name sample per source event; never persist transcripts."""
        try:
            stamp=time.time() if now is None else now
            if (name not in PHASES or type(seconds) not in (int,float) or not math.isfinite(seconds)
                    or not 0<=seconds<=86400 or type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp<0
                    or not all(isinstance(job.get(k),str) and 0<len(job[k])<=256 for k in ('account','username','event_id'))):return False
            scene=self.scene(job)
            event=hashlib.sha256((scene+'\0'+job['event_id']).encode()).hexdigest()
            with self.store._lock,self.store._conn:
                self.store._conn.execute('INSERT OR IGNORE INTO pipeline_phase VALUES(?,?,?,?,?)',(event,scene,name,seconds,stamp))
                self.store._conn.execute('DELETE FROM pipeline_phase WHERE updated<?',(stamp-14*86400,))
                self.store._conn.execute('DELETE FROM pipeline_phase WHERE rowid IN (SELECT rowid FROM pipeline_phase ORDER BY updated DESC,rowid DESC LIMIT -1 OFFSET 4096)')
            return True
        except Exception:return False

    def runtime(self,account,status,now=None):
        """Persist an observed status only; an old observation cannot replace a new one."""
        try:
            stamp=time.time() if now is None else now
            if (not isinstance(account,str) or not 0<len(account)<=256 or status not in RUNTIME
                    or type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp<0):return False
            account_hash=hashlib.sha256(account.encode()).hexdigest()
            self.store._write('INSERT INTO pipeline_runtime VALUES(?,?,?) ON CONFLICT(account_hash) DO UPDATE SET status=excluded.status,updated=excluded.updated WHERE excluded.updated>=pipeline_runtime.updated',
                              (account_hash,status,stamp))
            return True
        except Exception:return False

    def report(self,job,now=None):
        stamp=time.time() if now is None else now
        rows=self.store._rows('SELECT stage,reason FROM pipeline_trace WHERE scene_hash=? AND updated>=? AND updated<=?',(self.scene(job),stamp-86400,stamp))
        counts={stage:sum(row['stage']==stage for row in rows) for stage in STAGES}
        reasons={reason:sum(row['reason']==reason for row in rows) for reason in REASONS}
        lines=['本会话近24小时消息处理：','确认发送：'+str(counts['confirmed']),'送达未确认：'+str(counts['uncertain']),
               '排队或处理中：'+str(sum(counts[s] for s in ('queued','media','model','ready','sending','deferred','retry')))]
        for reason in ('model_silent','policy','quota','stale_turn','model_failed','media_unreadable','daily_cap','sticker_unavailable'):
            if reasons[reason]:lines.append(REASONS[reason]+'：'+str(reasons[reason]))
        for reason in ('obsolete','requeued','voice_timeout','voice_unavailable','offline','reader_error','account_mismatch','startup'):
            if reasons[reason]:lines.append(REASONS[reason]+'：'+str(reasons[reason]))
        phases=self.store._rows('SELECT phase,COUNT(*) AS n,AVG(seconds) AS average,MAX(seconds) AS maximum FROM pipeline_phase WHERE scene_hash=? AND updated>=? AND updated<=? GROUP BY phase',
                                (self.scene(job),stamp-86400,stamp))
        if phases:
            lines.append('本会话近24小时语音阶段耗时（各阶段可能重叠，不相加）：')
            by_name={row['phase']:row for row in phases}
            for name,label in PHASES.items():
                row=by_name.get(name)
                if row:lines.append(f"{label}：{row['n']}次，平均{row['average']:.2f}秒，最长{row['maximum']:.2f}秒")
        account_hash=hashlib.sha256(str(job.get('account','')).encode()).hexdigest()
        status=self.store._rows('SELECT status,updated FROM pipeline_runtime WHERE account_hash=? AND updated<=?',(account_hash,stamp))
        if not status:
            lines.append('运行状态：尚无检查记录，当前是否在线未知。')
        else:
            age=max(0,int(stamp-status[0]['updated']))
            label=RUNTIME.get(status[0]['status'],'未知')
            lines.append(f'运行状态：{label}（{age}秒前检查）'+('；状态已过期，当前是否在线未知。' if stamp-status[0]['updated']>300 else '。'))
        lines.append('送达未确认的消息不会自动重发。')
        return '\n'.join(lines)

    def purge(self,now=None):
        cutoff=(time.time() if now is None else now)-14*86400
        with self.store._lock,self.store._conn:
            for table in ('pipeline_trace','pipeline_phase','pipeline_runtime'):
                self.store._conn.execute('DELETE FROM '+table+' WHERE updated<?',(cutoff,))
