"""Durable original-sticker collection; never sends or clicks the UI."""
import hashlib,json,time
from sticker_media import metadata

class CollectQueue:
    def __init__(self,store,account):
        self.store=store;self.account=account
        store._write('CREATE TABLE IF NOT EXISTS sticker_collect(account TEXT,id TEXT,scene TEXT,source TEXT,local_id INTEGER,sender TEXT,ts REAL,hashes TEXT,state TEXT,attempts INTEGER,next_at REAL,PRIMARY KEY(account,id))')
        store._write("UPDATE sticker_collect SET state='pending' WHERE account=? AND state='processing'",(account,))

    def enqueue(self,scene,row,now=None):
        now=time.time() if now is None else now;m=row['message']
        if m.get('outgoing') or (m.get('type',0)&65535)!=47 or not now-600<=m.get('timestamp',0)<=now+60:return False
        hashes=m.get('sticker_hashes') or metadata(m.get('text',''))
        import re
        hashes=[h.lower() for h in hashes if isinstance(h,str) and re.fullmatch('[a-fA-F0-9]{32}',h)]
        if not hashes:return False
        identifier=hashlib.sha256((scene+'\0'+row['source']+'\0'+str(m['local_id'])).encode()).hexdigest()
        with self.store._lock,self.store._conn:
            result=self.store._conn.execute("INSERT OR IGNORE INTO sticker_collect SELECT ?,?,?,?,?,?,?,?,'pending',0,? WHERE (SELECT COUNT(*) FROM sticker_collect WHERE account=? AND state IN ('pending','processing'))<500",(self.account,identifier,scene,row['source'],m['local_id'],str(m.get('sender','')),m['timestamp'],json.dumps(hashes),now,self.account))
        return bool(result.rowcount)

    def claim(self,excluded,now=None):
        now=time.time() if now is None else now
        with self.store._lock,self.store._conn:
            rows=self.store._conn.execute("SELECT * FROM sticker_collect WHERE account=? AND state='pending' AND next_at<=? ORDER BY ts DESC LIMIT 500",(self.account,now)).fetchall()
            row=next((r for r in rows if r['scene'] not in excluded),None)
            if row is None:return None
            self.store._conn.execute("UPDATE sticker_collect SET state='processing',attempts=attempts+1 WHERE account=? AND id=?",(self.account,row['id']))
        m=dict(local_id=row['local_id'],type=47,outgoing=False,sender=row['sender'],timestamp=row['ts'],sticker_hashes=json.loads(row['hashes']))
        return dict(collect_id=row['id'],event_id='collect:'+row['id'],account=self.account,username=row['scene'],person_id=row['sender'],kind='group' if row['scene'].endswith('@chatroom') else 'private',batch=[dict(source=row['source'],event_id='collect:'+row['id'],message=m)])

    def finish(self,job,state,now=None):
        if state not in ('done','retry','rejected'):raise ValueError('invalid_collection_state')
        now=time.time() if now is None else now
        with self.store._lock,self.store._conn:
            row=self.store._conn.execute('SELECT attempts FROM sticker_collect WHERE account=? AND id=?',(self.account,job['collect_id'])).fetchone()
            if row is None:return
            result='pending' if state=='retry' and row['attempts']<4 else 'failed' if state=='retry' else state
            self.store._conn.execute('UPDATE sticker_collect SET state=?,next_at=? WHERE account=? AND id=?',(result,now+min(300,15*2**row['attempts']),self.account,job['collect_id']))
            self.store._conn.execute("DELETE FROM sticker_collect WHERE account=? AND state NOT IN ('pending','processing') AND ts<?",(self.account,now-86400))
