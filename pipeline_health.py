"""Scene-scoped message diagnostics containing hashes and fixed reason codes only."""
import hashlib,json,time

STAGES={'queued','media','model','ready','sending','confirmed','uncertain','deferred','retry','skipped','failed'}
REASONS={'claimed':'已入队','generating':'生成回复','media_read':'读取媒体','media_unreadable':'媒体未能读取','model_silent':'主动沉默','model_failed':'模型失败','policy':'策略跳过','quota':'频率限制','stale_turn':'已被新发言替代','unsupported':'不支持的消息','duplicate':'重复事件','sent':'确认送达','send_unknown':'送达未确认','send_retry':'发送前失败','waiting':'等待处理','command':'处理指令','sticker_unavailable':'表情暂不可用','daily_cap':'达到日限','group_capacity':'保留私聊通道'}

class PipelineHealth:
    def __init__(self,store,metrics=None):
        self.store=store;self.metrics=metrics
        store._write('CREATE TABLE IF NOT EXISTS pipeline_trace(event_hash TEXT PRIMARY KEY,scene_hash TEXT,kind TEXT,stage TEXT,reason TEXT,updated REAL,history TEXT)')
        store._write('CREATE INDEX IF NOT EXISTS pipeline_trace_scene ON pipeline_trace(scene_hash,updated)')

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

    def report(self,job,now=None):
        stamp=time.time() if now is None else now
        rows=self.store._rows('SELECT stage,reason FROM pipeline_trace WHERE scene_hash=? AND updated>=?',(self.scene(job),stamp-86400))
        counts={stage:sum(row['stage']==stage for row in rows) for stage in STAGES}
        reasons={reason:sum(row['reason']==reason for row in rows) for reason in REASONS}
        lines=['本会话近24小时消息处理：','确认发送：'+str(counts['confirmed']),'送达未确认：'+str(counts['uncertain']),
               '排队或处理中：'+str(sum(counts[s] for s in ('queued','media','model','ready','sending','deferred','retry')))]
        for reason in ('model_silent','policy','quota','stale_turn','model_failed','media_unreadable','daily_cap','sticker_unavailable'):
            if reasons[reason]:lines.append(REASONS[reason]+'：'+str(reasons[reason]))
        lines.append('送达未确认的消息不会自动重发。')
        return '\n'.join(lines)

    def purge(self,now=None):
        self.store._write('DELETE FROM pipeline_trace WHERE updated<?',((time.time() if now is None else now)-14*86400,))
