import hashlib,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import agent,bridge,settings
from conversation_memory import People,group_transcript
from sticker_library import StickerLibrary
from store import Store
from test_sticker_library import png


class GroupContextUpgradeTests(unittest.TestCase):
    def test_group_banter_does_not_cancel_answer_but_addressed_followup_does(self):
        from database_service import stale_group_reply,interrupts_parts
        cfg=settings.normalize({'policy':{'group_self_name':'小i'}});reader=Mock()
        job=dict(kind='group',is_mention=False,person_id='a',account='self',username='room',group_reply_mode='interject',batch=[dict(source='s',message={'local_id':1})])
        banter=dict(local_id=2,sender='b',text='今天好热',outgoing=False)
        reader.messages.return_value=[banter]
        self.assertFalse(interrupts_parts(banter,job,bridge,cfg))
        self.assertFalse(stale_group_reply(reader,job,bridge,cfg))
        for fresh in (dict(banter,sender='a',text='等等改一下'),dict(banter,text='@小i 等等'),dict(banter,quote_sender='self')):
            reader.messages.return_value=[fresh]
            self.assertTrue(stale_group_reply(reader,job,bridge,cfg))
            self.assertTrue(interrupts_parts(fresh,job,bridge,cfg))
        self.assertTrue(interrupts_parts(banter,dict(job,kind='private'),bridge,cfg))

    def test_frequency_config_is_bounded_and_bad_values_fall_back(self):
        cfg=settings.normalize({'stickers':{'send_probability':float('nan'),'min_interval_seconds':-3,'same_sticker_interval_seconds':'bad'},'policy':{'group_check_min_seconds':0}})
        self.assertEqual(cfg['stickers']['send_probability'],.3)
        self.assertEqual(cfg['stickers']['min_interval_seconds'],10)
        self.assertEqual(cfg['stickers']['same_sticker_interval_seconds'],86400)
        self.assertEqual(cfg['policy']['group_check_min_seconds'],3)
    def test_higher_sticker_frequency_keeps_unknown_delivery_blocked(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store();lib=StickerLibrary(store,root,'self')
            try:
                raw=png();asset=lib.collect(raw,[hashlib.md5(raw).hexdigest()],'小猫开心大笑')
                lib.set_native(asset,'f'*32,'confirmed')
                event=next(str(i) for i in range(1000) if 30<=int(hashlib.sha256(str(i).encode()).hexdigest()[:8],16)%100<50)
                frequency=dict(send_probability=.5,min_interval_seconds=300,same_sticker_interval_seconds=1800)
                with patch('sticker_library.time.time',return_value=10000):
                    self.assertIsNotNone(lib.choose('哈哈','开心','s',event,frequency=frequency))
                for age,want in ((240,False),(600,False),(1801,True)):
                    store._write('DELETE FROM sticker_usage')
                    with patch('sticker_library.time.time',return_value=10000-age):lib.record_send(asset,'s','old','confirmed')
                    with patch('sticker_library.time.time',return_value=10000):
                        self.assertEqual(lib.choose('哈哈','开心','s',event,frequency=frequency) is not None,want)
                store._write('DELETE FROM sticker_usage')
                with patch('sticker_library.time.time',return_value=5000):lib.record_send(asset,'s','unknown','unconfirmed')
                with patch('sticker_library.time.time',return_value=10000):
                    self.assertIsNone(lib.choose('哈哈','开心','s',event,frequency=frequency))
            finally:store.close()

    def test_group_reads_earlier_arrangement_without_future_messages(self):
        rows=[dict(local_id=i,type=1,text='集合地点是图书馆东门，七点半' if i==1 else '聊天'+str(i),sender='a' if i%2 else 'b',timestamp=i,outgoing=False) for i in range(1,47)]
        rows[-1]['text']='未来消息不属于这轮'
        job=dict(account='self',username='room@chatroom',batch=[dict(source='message/message_0.db',message=rows[-2])])
        reader=Mock();reader.messages.return_value=rows
        text=group_transcript(reader,job,{'a':'甲','b':'乙'})
        self.assertIn('图书馆东门',text);self.assertNotIn('未来消息',text)

    def test_group_media_uses_only_observations_from_the_same_scene(self):
        store=Store();people=People(store)
        try:
            m=dict(local_id=1,type=47,text='media',sender='a',timestamp=1,outgoing=False,server_id='123')
            people.observe('self','room@chatroom','s',m,'小猫抱着蓝鲸笑','original')
            people.observe('self','private','s',m,'私聊秘密内容','original')
            reader=Mock();reader.messages.return_value=[m]
            job=dict(account='self',username='room@chatroom',batch=[dict(source='s',message=m)])
            text=group_transcript(reader,job,{},people=people)
            self.assertIn('小猫抱着蓝鲸笑',text);self.assertNotIn('私聊秘密',text)
        finally:store.close()

    def test_open_questions_are_judged_without_lottery_but_still_rate_limited(self):
        cfg=settings.normalize({'policy':{'group_interject_probability':.2,'group_interject_min_seconds':120}})
        job=dict(identity='room',event_id='ffff0000',focus_text='大家知道怎么处理这个问题吗？',account='self',is_mention=False)
        self.assertTrue(bridge.should_interject(job,cfg,{},1000,checked_map={}))
        self.assertFalse(bridge.should_interject(job,cfg,{},1000,checked_map={'room':990}))
        self.assertFalse(bridge.should_interject(job,cfg,{'room':950},1000,checked_map={}))
        self.assertFalse(bridge.should_interject(dict(job,quote_sender='other'),cfg,{},1000,checked_map={}))
        self.assertFalse(bridge.should_interject(dict(job,focus_text='@别人 你觉得呢？'),cfg,{},1000,checked_map={}))
        self.assertFalse(bridge.should_interject(dict(job,focus_text='天气真好'),cfg,{},1000,checked_map={}))
        self.assertFalse(bridge.should_interject(dict(job,focus={'message':{'timestamp':time.time()-300}}),cfg,{},1000,checked_map={}))

    def test_only_actual_group_reply_updates_speaking_and_engagement(self):
        from conversation_memory import remember_group_reply
        cfg=settings.normalize({});engaged={};topics={};sent={}
        job=dict(kind='group',username='room',identity='room',person_id='a',focus_text='猫咪',group_reply_mode='interject')
        remember_group_reply(job,'猫可以吃这个',cfg['policy'],bridge,engaged,topics,sent,1000)
        self.assertEqual(sent['room'],1000);self.assertGreater(engaged['room','a'],1000)
        self.assertIn('猫咪',topics['room','a']);self.assertNotIn('猫可',topics['room','a'])

    def test_full_api_payload_retains_group_background_and_current_question(self):
        cfg=settings.normalize({'tools':{'enabled':False},'memory':{'summary_every':0}})
        store=Store();client=Mock()
        client.chat.return_value=dict(content='七点半到图书馆东门集合。',tool_calls=[],tokens_in=1,tokens_out=1)
        try:
            with patch('bridge.agent.ModelClient',return_value=client):
                reply=bridge.build_responder(cfg,store,'unused',{'text':'persona'})
                reply(dict(session_key='room',kind='group',message='我们几点在哪集合？',focus_text='我们几点在哪集合？',group_context='早先约定：七点半图书馆东门\n'+'中间闲聊内容'*800+'\n最近：大家已经准备出发',group_reply_mode='mention'))
            content=client.chat.call_args.args[0][-1]['content']
            self.assertIn('七点半图书馆东门',content);self.assertIn('我们几点在哪集合',content);self.assertIn('准备出发',content)
            self.assertLessEqual(len(content),8000)
            messages=agent.build_messages('p',[],[],'x'*9000,'now')
            self.assertEqual(len(messages[-1]['content']),4000)
        finally:store.close()
