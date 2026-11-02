import tempfile
import unittest
from types import SimpleNamespace
from database_service import obsolete_reply
from event_pipeline import Inbox
from store import Store


class ObsoleteReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        path=self.tmp.name+'/state.db'
        self.store=Store(path,14);self.addCleanup(self.store.close)
        self.inbox=Inbox(path);self.addCleanup(self.inbox.close)
        self.b=SimpleNamespace(mentioned_self=lambda text,names:'@小i' in text,policy_self_names=lambda cfg:['小i'])
        self.message=dict(local_id=1,outgoing=False,timestamp=100,text='old',sender='person',type=1)
        self.inbox.ingest('a','p','s','title',[self.message],4)
        batch=self.inbox.claim(set(),now=101,account='a')
        self.job=dict(account='a',username='p',session_key='k',event_id=batch[0]['batch_id'],batch=batch,kind='private',person_id='person')
        self.new=dict(self.message,local_id=2,text='actually new',timestamp=102)
        self.reader=SimpleNamespace(messages=lambda *args,**kw:[self.new])

    def check(self):
        return obsolete_reply(self.reader,self.job,self.inbox,self.store,self.b,{})

    def test_wait_until_ingested(self):
        self.assertEqual(self.check(),'wait')

    def test_filtered_late_history_does_not_wait_forever(self):
        self.new['timestamp']=99
        self.inbox.ingest('a','p','s','title',[self.new],4,minimum_timestamp=100)
        self.assertEqual(self.check(),'')

    def test_new_input_supersedes_unsent_draft(self):
        self.inbox.ingest('a','p','s','title',[self.new],4)
        self.assertEqual(self.check(),'obsolete')

    def test_submitted_or_queued_plan_preserved(self):
        for event in (self.job['event_id'],self.job['event_id']+':part:0','action:'+self.job['event_id'],'sticker:'+self.job['event_id']):
            self.store.start_delivery(event,'k',event,'old','draft')
            self.assertEqual(self.check(),'')

    def test_commands_are_not_cancelled(self):
        self.job['memory_response']=True
        self.assertEqual(self.check(),'')

    def test_other_group_member_does_not_cancel_direct_mention(self):
        self.job.update(kind='group',is_mention=True,group_reply_mode='mention')
        self.new['sender']='another'
        self.assertEqual(self.check(),'')

    def test_group_spontaneous_draft_yields_to_new_discussion(self):
        self.job.update(kind='group',group_reply_mode='interject')
        self.new['sender']='another'
        self.inbox.ingest('a','p','s','title',[self.new],4)
        self.assertEqual(self.check(),'obsolete')

    def test_no_new_input(self):
        self.reader.messages=lambda *a,**k:[]
        self.assertEqual(self.check(),'')

    def test_command_receipt_marker_survives_restart(self):
        from reply_plan import freeze_reply,restore_reply
        restored={}
        self.assertEqual(restore_reply(restored,freeze_reply({'command_response':True},'已取消任务')),'已取消任务')
        self.assertTrue(restored['command_response'])

    def test_strict_mention_supplement_does_not_remove_authorized_reply(self):
        self.job.update(kind='group',is_mention=True,group_reply_mode='mention')
        self.inbox.ingest('a','p','s','title',[self.new],3)
        cfg={'policy':{'group_strict_mention':True}}
        self.assertEqual(obsolete_reply(self.reader,self.job,self.inbox,self.store,self.b,cfg),'')
        self.new['text']='@小i 实际上换个问题'
        self.assertEqual(obsolete_reply(self.reader,self.job,self.inbox,self.store,self.b,cfg),'obsolete')
