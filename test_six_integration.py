import unittest,time
from unittest.mock import Mock,patch
import agent,bridge,settings,database_service as service
from store import Store
from tasks import Tasks


class SixIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.store=Store();self.tasks=Tasks(self.store);self.cfg=settings.normalize({'policy':{'group_self_name':'小i'}})
        self.job=dict(account='self',person_id='a',username='a',title='甲',session_key='s',kind='private',event_id='inbound',is_mention=False,focus_text='10秒后提醒我喝水',self_names=['小i'],focus=dict(event_id='stable-inbound',message=dict(text='10秒后提醒我喝水',sender='a',type=1,outgoing=False)))
    def tearDown(self):self.store.close()

    def test_semantic_none_never_falls_back_to_keyword_sticker(self):
        lib=Mock();job=dict(structured_reply=True,sticker_choice=None,message='哈哈',username='s')
        self.assertIsNone(service.choose_sticker_asset(lib,job,'哈哈',self.cfg,None,False,'e'))
        lib.choose.assert_not_called()
        job['sticker_choice']='a'*64
        service.choose_sticker_asset(lib,job,'哈哈',self.cfg,None,False,'e')
        self.assertEqual(lib.choose.call_args.kwargs['semantic_id'],'a'*64)

    def test_sticker_only_success_and_failure_never_send_marker_as_text(self):
        job=dict(self.job,event_id='evt',batch=[],sticker_only=True,sticker_choice='a'*64,structured_reply=True)
        for state,want in (('confirmed','done'),('unconfirmed','unconfirmed'),('skipped','done')):
            with patch.object(service,'send_sticker',return_value=state),patch.object(service,'send_reply') as text:
                result=service.deliver_sticker_only(None,None,job,agent.STICKER_ONLY,Mock(),self.store,bridge,self.cfg)
            self.assertEqual(result,want);text.assert_not_called()

    def test_due_reminder_waits_for_private_messages_and_uses_same_scene(self):
        self.tasks.handle(self.job,now=1000)
        inbox=Mock();inbox.count.return_value=1
        reader=Mock();reader.account='self'
        with patch.object(service,'send_reply') as send:
            self.assertFalse(service.run_due_task(None,reader,self.tasks,self.store,inbox,bridge,self.cfg,now=1011))
        send.assert_not_called()
        inbox.count.return_value=0;reader.contacts.return_value={'a':'甲'}
        with patch.object(service,'send_reply',return_value='done') as send,patch('bridge.quota_wait',return_value=0),patch('bridge.policy.allowed',return_value=(True,'')):
            self.assertTrue(service.run_due_task(None,reader,self.tasks,self.store,inbox,bridge,self.cfg,now=1011))
        sent_job=send.call_args.args[2]
        self.assertEqual(sent_job['username'],'a');self.assertEqual(sent_job['account'],'self')
        self.assertEqual(self.store._rows('SELECT state FROM tasks')[0]['state'],'confirmed')

    def test_unknown_reminder_is_not_replayed_or_cancelled_by_others(self):
        self.tasks.handle(self.job,now=1000)
        inbox=Mock();inbox.count.return_value=0;reader=Mock();reader.account='self';reader.contacts.return_value={}
        with patch.object(service,'send_reply',return_value='unconfirmed'),patch('bridge.quota_wait',return_value=0),patch('bridge.policy.allowed',return_value=(True,'')):
            self.assertTrue(service.run_due_task(None,reader,self.tasks,self.store,inbox,bridge,self.cfg,now=1011))
            self.assertFalse(service.run_due_task(None,reader,self.tasks,self.store,inbox,bridge,self.cfg,now=1012))
        self.tasks.recover();self.assertEqual(self.tasks.due('self',now=1013),[])

    def test_current_batch_cannot_reintroduce_forgotten_quote(self):
        message=dict(local_id=4,type=1,sender='b',text='这句话什么意思',quote_sender='a',quote_text='需要遗忘的旧秘密',timestamp=1000)
        job=dict(self.job,kind='group',username='r@chatroom',focus_text=message['text'],message=message['text'],person_id='b',voice_mode=False,force_reply=False,
                 batch=[dict(source='s',event_id='evt',message=message)],sender_names={'a':'甲','b':'乙'})
        people=Mock();people.source_valid.return_value=False
        captured=[]
        reply=service.prepare_model(job,None,lambda j:captured.append(j['message']) or '我可以看你新发的说明',None,bridge,self.cfg,people=people)
        self.assertNotIn('需要遗忘的旧秘密',captured[0])
        self.assertIn('这句话什么意思',captured[0])

if __name__=='__main__':unittest.main()
