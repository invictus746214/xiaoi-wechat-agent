import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import agent
import bridge
import settings
from conversation_memory import People, group_turn, shared_statement, group_transcript
from database_service import build_job, interrupts_parts, stale_group_reply
from event_pipeline import Inbox, Reader
from store import Store
from wechat_db import quote_metadata


def message(n,sender,text):
    return dict(local_id=n,sender=sender,text=text,type=1,timestamp=time.time()+n,outgoing=False)


class IdentityGroupTests(unittest.TestCase):
    def setUp(self):
        self.cfg=settings.normalize({'policy':{'group_self_name':'小i'}})
        self.inbox=Inbox(':memory:')
        self.store=Store()

    def tearDown(self):
        self.inbox.close()
        self.store.close()

    def batch(self,rows):
        self.inbox.ingest('self','room@chatroom','message/message_0.db','group',rows,3)
        return self.inbox.claim(set(),now=time.time()+100)

    def test_group_batches_stop_at_speaker_change_and_retry_is_frozen(self):
        batch=self.batch([message(1,'a','@小i 猫'),message(2,'a','猫怎么样'),message(3,'b','吃饭')])
        self.assertEqual([r['message']['local_id'] for r in batch],[1,2])
        event=batch[0]['batch_id']
        self.inbox.complete(batch,'retry',now=0)
        retry=self.inbox.claim(set(),now=time.time()+100)
        self.assertEqual(retry[0]['batch_id'],event)
        self.assertEqual(len(retry),2)
        self.inbox.complete(retry,'done')
        self.assertEqual(self.inbox.claim(set(),now=time.time()+100)[0]['message']['sender'],'b')

    def test_actual_mention_author_is_reply_target_even_in_old_mixed_batch(self):
        batch=self.batch([message(1,'a','@小i 猫')])
        batch.append(dict(batch[0],message=message(2,'b','吃饭')))
        job=build_job(batch,self.inbox,bridge,self.cfg,'self')
        self.assertEqual(job['person_id'],'a')
        self.assertIn('猫',job['focus_text'])
        self.assertTrue(job['is_mention'])
        batch[0]['message']=dict(message(1,'a','你觉得呢'),quote_sender='self')
        self.assertEqual(build_job(batch,self.inbox,bridge,self.cfg,'self')['person_id'],'a')

    def test_engagement_is_person_specific_and_quote_to_other_is_not_for_bot(self):
        engaged={}; topics={}
        behavior=SimpleNamespace(message_terms=lambda text:{'cat'},related_to_topic=lambda terms,text:bool(terms))
        job=dict(username='room',person_id='a',focus_text='cat',is_mention=True)
        group_turn(job,'self',self.cfg['policy'],behavior,engaged,topics,100)
        job['is_mention']=False
        self.assertEqual(group_turn(job,'self',self.cfg['policy'],behavior,engaged,topics,101),(True,False))
        self.assertEqual(group_turn(dict(job,person_id='b'),'self',self.cfg['policy'],behavior,engaged,topics,101),(False,True))
        self.assertEqual(group_turn(dict(job,quote_sender='b'),'self',self.cfg['policy'],behavior,engaged,topics,101),(False,False))

    def test_explicit_group_reply_survives_other_speakers_but_not_followups(self):
        job=dict(kind='group',is_mention=True,person_id='a',account='self')
        self.assertFalse(interrupts_parts(message(2,'b','吃饭'),job,bridge,self.cfg))
        self.assertTrue(interrupts_parts(message(2,'a','等等'),job,bridge,self.cfg))
        self.assertTrue(interrupts_parts(message(2,'b','@小i 等等'),job,bridge,self.cfg))
        self.assertTrue(interrupts_parts(dict(message(2,'b','等等'),quote_sender='self'),job,bridge,self.cfg))
        reader=Mock()
        reader.messages.return_value=[message(2,'b','新话题')]
        job.update(username='room',batch=[dict(source='message/message_0.db',message=message(1,'a','猫'))],group_reply_mode='interject')
        self.assertFalse(stale_group_reply(reader,job,bridge,self.cfg))
        self.assertFalse(stale_group_reply(reader,dict(job,is_mention=False),bridge,self.cfg))

    def test_shared_preferences_are_conservative_and_name_changes_follow_stable_id(self):
        self.assertIsNone(shared_statement('我喜欢和秘密联系人约会'))
        self.assertIsNone(shared_statement('我喜欢工资8000的工作'))
        self.assertIsNotNone(shared_statement('我喜欢猫和机器人'))
        people=People(self.store)
        people.record('self','a','旧名',[message(1,'a','叫我小杜')],'message/message_0.db',{})
        people.record('self','room@chatroom','group',[message(2,'a','我喜欢猫')],'message/message_0.db',{'a':'新名'})
        context=people.context('self','a','another@chatroom','我是谁')
        self.assertIn('新名',context)
        self.assertIn('小杜',context)
        self.assertNotIn('小杜',people.context('self','b','another@chatroom','我是谁'))
        people.record('self','a','甲',[message(3,'a','我喜欢猫和狗'),message(4,'a','我不喜欢猫')],'message/message_0.db',{})
        facts=[r['value'] for r in self.store._rows('SELECT value FROM people_shared')]
        self.assertIn('不喜欢猫',facts)
        self.assertIn('喜欢狗',facts)
        self.assertNotIn('喜欢猫',facts)
        self.assertNotIn('喜欢猫和狗',facts)

    def test_transcript_attribution_quote_and_future_bound(self):
        batch=self.batch([message(2,'a','你觉得呢')])
        job=build_job(batch,self.inbox,bridge,self.cfg,'self')
        reader=Mock()
        reader.messages.return_value=[message(1,'b','猫'),dict(message(2,'a','你觉得呢'),quote_sender='b',quote_text='猫'),message(3,'b','未来消息')]
        transcript=group_transcript(reader,job,{'a':'甲','b':'乙'})
        self.assertIn('[甲#',transcript)
        self.assertIn('[乙#',transcript)
        self.assertIn('引用 乙#',transcript)
        self.assertNotIn('未来消息',transcript)

    def test_memory_seed_does_not_queue_historic_read_messages(self):
        rows=[message(1,'a','叫我小杜')]
        db=SimpleNamespace(account='self',keys={'message/message_0.db':{}},
            sessions=lambda limit:[dict(username='a',title='甲',unread=0,last_timestamp=100)],
            messages=lambda user,limit,source,after_id=None:rows if after_id is None else [m for m in rows if m['local_id']>after_id])
        callback=Mock()
        worker=Reader(lambda:db,self.inbox,self.cfg,lambda _:False,callback)
        self.assertEqual(worker.poll(db),0)
        callback.assert_called_once()
        rows.append(message(2,'a','新消息'))
        callback.side_effect=RuntimeError('memory unavailable')
        db.sessions=lambda limit:[dict(username='a',title='甲',unread=1,last_timestamp=101)]
        with self.assertRaises(RuntimeError): worker.poll(db)
        self.assertEqual(self.inbox.cursor('self','a','message/message_0.db'),1)
        callback.side_effect=None
        self.assertEqual(worker.poll(db),1)

    def test_group_agent_ignores_legacy_unattributed_memory(self):
        self.store.add_message('room','user','legacy-secret')
        client=Mock()
        client.chat.return_value=dict(content='当然记得，你喜欢猫。',tool_calls=[],tokens_in=1,tokens_out=1)
        agent.respond(client,'persona',self.store,'room','问题',conversation_context='称呼：小杜，喜欢猫',scene_kind='group')
        payload=str(client.chat.call_args.args[0])
        self.assertNotIn('legacy-secret',payload)
        self.assertIn('小杜',payload)
        agent.respond(client,'persona',self.store,'room','问题')
        self.assertIn('legacy-secret',str(client.chat.call_args.args[0]))

    def test_interject_has_silence_policy_no_tools_and_no_legacy_summary(self):
        with patch('bridge.agent.respond',return_value='SILENT') as respond, patch('bridge.agent.maybe_summarize') as summarize:
            reply=bridge.build_responder(self.cfg,self.store,'unused',{'text':'persona'})
            reply(dict(session_key='room',kind='group',group_reply_mode='interject',message='本轮媒体观察',target_label='乙',focus_text='本轮问题',group_context='很长的历史'*1000))
        self.assertIn('只输出 SILENT',respond.call_args.args[1])
        self.assertIsNone(respond.call_args.kwargs['tools'])
        self.assertEqual(respond.call_args.kwargs['scene_kind'],'group')
        prompt=respond.call_args.args[4][:4000]
        self.assertIn('本轮问题',prompt)
        self.assertIn('本轮媒体观察',prompt)
        self.assertLessEqual(len(respond.call_args.args[4]),8000)
        summarize.assert_not_called()


class QuoteTests(unittest.TestCase):
    def xml(self,source='room@chatroom',member='a',kind='1'):
        return '<msg><appmsg><type>57</type><title>我觉得可以</title><refermsg><fromusr>'+source+'</fromusr><chatusr>'+member+'</chatusr><type>'+kind+'</type><content>原话</content><svrid>123</svrid></refermsg></appmsg></msg>'

    def test_group_quoted_member_and_private_sender(self):
        data=quote_metadata('b:\n'+self.xml(),'b')
        self.assertEqual(data['quote_sender'],'a')
        self.assertEqual(data['text'],'我觉得可以')
        self.assertEqual(data['quote_text'],'原话')
        self.assertEqual(quote_metadata(self.xml('friend'),'b')['quote_sender'],'friend')
        self.assertNotIn('原话',quote_metadata(self.xml(kind='3'),'b')['quote_text'])

    def test_invalid_and_entity_xml_are_rejected(self):
        self.assertIsNone(quote_metadata('not XML','a'))
        self.assertIsNone(quote_metadata('<!DOCTYPE msg>'+self.xml(),'a'))
        self.assertIsNone(quote_metadata(self.xml().replace('<type>57</type>','<type>6</type>'),'a'))


if __name__=='__main__': unittest.main()
