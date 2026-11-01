import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bridge
import database_service as service
import settings
import agent
from conversation_memory import People, group_turn, shared_statement
from event_pipeline import Inbox
from moments_agent import Runner
from store import Store
from test_moments_agent import FakeFeed, config, post


def msg(n, text, sender='a', kind=1, stamp=None):
    return dict(local_id=n, text=text, sender=sender, type=kind, timestamp=stamp or time.time()+n,
                outgoing=False, server_id=str(n+100))


class SixUpdatesTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.people = People(self.store)
        self.cfg = settings.normalize({})

    def tearDown(self):
        self.store.close()

    def record(self, n, text, scene='a', stamp=None):
        self.people.record('self', scene, '甲', [msg(n,text,stamp=stamp)], 'message/message_0.db', {'a':'甲'})

    def test_corrections_and_forgetting_survive_history_replay(self):
        self.assertEqual(shared_statement('以后叫我小林。'), ('name','称呼：小林'))
        self.assertEqual(shared_statement('我现在不喜欢猫了'), ('interest:猫','不喜欢猫'))
        self.record(1,'叫我小林')
        self.record(2,'我喜欢猫')
        self.record(3,'我现在不喜欢猫了')
        self.assertIn('不喜欢猫', self.people.context('self','a','room@chatroom','猫'))
        self.record(4,'忘记我喜欢猫')
        self.assertNotIn('不喜欢猫', self.people.context('self','a','room@chatroom','猫'))
        self.assertNotIn('我喜欢猫', self.people.context('self','a','a','猫'))
        self.record(2,'我喜欢猫')
        self.assertNotIn('喜欢猫', self.people.context('self','a','a','猫'))
        self.assertTrue(self.people.suppress_legacy('self','a'))
        self.assertFalse(self.people.suppress_legacy('self','b'))
        self.record(5,'我喜欢狗')
        self.assertIn('喜欢狗', self.people.context('self','a','room@chatroom','兴趣'))

    def test_forget_all_is_person_scoped_and_has_persistent_cutoff(self):
        self.record(1,'我喜欢猫')
        self.people.record('self','room@chatroom','群',[msg(2,'我喜欢狗','b')],'message/message_0.db',{})
        self.record(3,'忘记关于我的所有记忆')
        self.record(1,'我喜欢猫')
        self.assertNotIn('喜欢猫',self.people.context('self','a','a','猫'))
        self.assertIn('喜欢狗',self.people.context('self','b','room@chatroom','狗'))
        self.assertIn('已清除',self.people.memory_reply('忘记关于我的所有记忆'))
        self.assertEqual(self.people.memory_reply('他说忘记我喜欢猫'), '')

    def test_visual_observations_are_message_and_scene_scoped_and_failures_retry(self):
        message=msg(1,'',kind=3)
        self.people.observe('self','a','message/message_0.db',message,'蓝鲸抱枕','original')
        self.assertIn('蓝鲸抱枕',self.people.context('self','a','a','刚才图片'))
        self.assertNotIn('蓝鲸抱枕',self.people.context('self','a','room@chatroom','图片'))
        self.assertEqual(self.people.observation('self','a','message/message_0.db',message),'蓝鲸抱枕')
        self.assertEqual(self.people.observation('other','a','message/message_0.db',message),'')
        self.people.observe('self','a','message/message_0.db',msg(2,'',kind=3),'','missing')
        self.assertEqual(self.people.observation('self','a','message/message_0.db',msg(2,'',kind=3)), '')

    def test_group_other_addressee_and_short_followup(self):
        policy=self.cfg['policy']
        engaged={('room','a'):200};topics={('room','a'):{'猫'}}
        job=dict(username='room',person_id='a',focus_text='那为什么呢',is_mention=False,group_context='')
        self.assertEqual(group_turn(job,'self',policy,bridge,engaged,topics,100),(True,False))
        self.assertEqual(group_turn(dict(job,focus_text='@小林 你觉得呢'),'self',policy,bridge,engaged,topics,100),(False,False))
        self.assertEqual(group_turn(dict(job,quote_sender='b'),'self',policy,bridge,engaged,topics,100),(False,False))
        self.assertEqual(group_turn(dict(job,is_mention=True),'self',policy,bridge,engaged,topics,100)[0],True)

    def test_background_has_bounded_slot_under_group_load_but_urgent_wins(self):
        inbox=Inbox(':memory:')
        try:
            inbox.ingest('self','room@chatroom','s','群',[msg(1,'闲聊')],1)
            self.assertTrue(service.background_allowed(inbox,'self',due=100,now=221,busy=True))
            self.assertFalse(service.background_allowed(inbox,'self',due=100,now=120,busy=True))
            inbox.ingest('self','a','s','甲',[msg(1,'新私聊')],4)
            self.assertFalse(service.background_allowed(inbox,'self',due=100,now=221,busy=True))
            self.assertFalse(service.background_allowed(inbox,'other',due=300,now=221,busy=False))
        finally:
            inbox.close()

    def test_command_receipt_persists_and_replay_never_executes_twice(self):
        reader=Mock(account='self')
        reader.moments.side_effect=[[dict(id='p',username='a',likes=[],thread=[])],[dict(id='p',username='a',likes=['self'],thread=[])]]
        behavior=SimpleNamespace(run_moment_command=Mock(return_value='UI声称完成'))
        job=dict(command='like',event_id='e',session_key='k',username='a',message='点赞',comment_text='')
        result=service.execute_command(None,reader,job,self.store,behavior,self.cfg)
        self.assertIn('赞',result)
        self.assertEqual(self.store.delivery('action:e')['state'],'confirmed')
        self.assertEqual(service.execute_command(None,reader,job,self.store,behavior,self.cfg), result)
        behavior.run_moment_command.assert_called_once()

    def test_command_no_native_receipt_never_claims_success(self):
        reader=Mock(account='self')
        reader.moments.return_value=[dict(id='p',username='a',text='动态',thread=[],likes=[])]
        behavior=SimpleNamespace(run_moment_command=Mock(return_value='评论好了。'))
        job=dict(command='comment',event_id='e',session_key='k',username='a',message='评论',comment_text='你好')
        with patch.object(service.time,'sleep'):
            result=service.execute_command(None,reader,job,self.store,behavior,self.cfg)
        self.assertIn('未确认',result)
        self.assertNotIn('评论好了',result)
        self.assertEqual(self.store.delivery('action:e')['state'],'unconfirmed')

    def test_auto_comment_repetition_author_gap_and_unknown_survive_restart(self):
        cfg=config(comment_enabled=True,like_enabled=False,dry_run=False)
        cfg['moments']['comment_author_interval_seconds']=21600
        item=post('甲','今天看猫')
        feed=FakeFeed([item])
        Runner(feed,cfg,self.store,lambda _: '猫猫好可爱',clock=lambda:time.time()).run()
        second=FakeFeed([post('甲','明天看狗')])
        report=Runner(second,cfg,self.store,lambda _: '狗狗好可爱').run()
        self.assertEqual(second.calls,[])
        self.assertTrue(any('author_interval' in reason for _,_,reason in report.skipped))
        repeat=FakeFeed([post('乙','后天看猫')])
        Runner(repeat,cfg,self.store,lambda _: '猫猫好可爱！').run()
        self.assertEqual(repeat.calls,[])

    def test_runner_action_limit_and_quota_count_uncertain(self):
        cfg=config(comment_enabled=True,like_enabled=True,dry_run=False,comment_daily_cap=1)
        first=FakeFeed([post('甲','猫')],comment_result='uncertain')
        Runner(first,cfg,self.store,lambda _:'好可爱的猫').run(max_actions=1)
        self.assertEqual(len(first.calls),1)
        unknown=FakeFeed([post('乙','狗')],comment_result='uncertain')
        Runner(unknown,cfg,self.store,lambda _:'好可爱的狗').run()
        blocked=FakeFeed([post('丙','花')])
        Runner(blocked,cfg,self.store,lambda _:'花开得真好').run()
        self.assertFalse(any(call[0]=='comment' for call in blocked.calls))

    def test_forgetting_filters_legacy_prompt_and_removes_private_derived_memory(self):
        inbox=Inbox(':memory:')
        try:
            self.store._conn.execute('CREATE TABLE conversation_alias(account TEXT,username TEXT,memory_key TEXT)')
            self.store._conn.execute("INSERT INTO conversation_alias VALUES('self','a','private-key')")
            self.store.add_message('private-key','user','我喜欢猫')
            self.store.add_fact('private-key','我喜欢猫')
            self.record(1,'我喜欢猫')
            self.record(2,'忘记我喜欢猫')
            self.assertEqual(self.store.facts('private-key'),[])
            self.assertEqual(self.store.recent('private-key',10),[])
            self.store.add_message('private-key','assistant','旧摘要里的猫秘密')
            client=Mock()
            client.chat.return_value=dict(content='现在知道啦',tool_calls=[],tokens_in=1,tokens_out=1)
            agent.respond(client,'persona',self.store,'private-key','你好',suppress_legacy=True)
            self.assertNotIn('旧摘要里的猫秘密',str(client.chat.call_args.args[0]))
        finally:
            inbox.close()

    def test_cached_vision_is_used_by_actual_prepare_model_without_second_api_call(self):
        message=msg(1,'',kind=3)
        self.people.observe('self','a','s',message,'蓝鲸抱枕','original')
        job=dict(account='self',username='a',event_id='ev',batch=[dict(event_id='ev',source='s',message=message)],
                 force_reply=False,voice_mode=False,kind='private')
        responder=Mock(return_value='抱枕很可爱')
        vision=Mock(side_effect=AssertionError('should reuse'))
        service.prepare_model(job,Mock(),responder,vision,bridge,self.cfg,self.people)
        self.assertIn('蓝鲸抱枕',responder.call_args.args[0]['message'])
        vision.assert_not_called()

    def test_activity_survives_restart_and_blocks_unknown_replay(self):
        with tempfile.TemporaryDirectory() as root:
            path=str(Path(root)/'state.db')
            cfg=config(like_enabled=False,comment_enabled=True,dry_run=False)
            target=post('甲','重启测试')
            state=Store(path)
            state.reserve_moment(target,'comment','很漂亮的风景','sending')
            state.close()
            state=Store(path)
            try:
                feed=FakeFeed([target])
                Runner(feed,cfg,state,lambda _:'另一条评论').run()
                self.assertEqual(feed.calls,[])
                self.assertEqual(state.moments_today('comment'),1)
            finally:
                state.close()

    def test_quoted_other_speaker_cannot_become_bot_reply_in_prompt(self):
        from conversation_memory import group_focus
        job=dict(account='self',username='room@chatroom',person_id='a',quote_sender='b',is_mention=True)
        focus=group_focus(job,'self',{'a':'甲','b':'乙'})
        self.assertIn('本轮发言人：甲',focus)
        self.assertIn('明确对话对象：小i',focus)
        self.assertIn('引用来源：乙',focus)

    def test_model_cannot_save_memory_without_tools_on_forget_turn(self):
        self.record(1,'忘记关于我的所有记忆')
        self.assertTrue(self.people.suppress_legacy('self','a'))
        self.record(2,'以后叫我小林')
        self.assertIn('小林',self.people.context('self','a','a','我是谁'))

    def test_auto_comment_ui_closed_without_native_receipt_is_uncertain(self):
        cfg=config(like_enabled=False,comment_enabled=True,dry_run=False)
        target=post('甲','猫猫在睡觉')
        reader=Mock(account='self')
        reader.contacts.return_value={'a':'甲'}
        reader.moments.return_value=[dict(id='native',username='a',text=target.text,thread=[],likes=[])]
        feed=FakeFeed([target])
        Runner(feed,cfg,self.store,lambda _:'睡得真香',pause=lambda _:None,reader=reader).run()
        self.assertEqual(self.store._rows('SELECT state FROM moment_activity')[0]['state'],'uncertain')
        self.assertTrue(self.store.moment_done(target.id,'comment_uncertain'))

    def test_auto_comment_native_account_and_target_required(self):
        cfg=config(like_enabled=False,comment_enabled=True,dry_run=False)
        target=post('小i','自己发的动态')
        reader=Mock(account='self')
        reader.contacts.return_value={'self':'小i'}
        reader.moments.return_value=[dict(id='own',username='self',text=target.text,thread=[],likes=[])]
        feed=FakeFeed([target])
        Runner(feed,cfg,self.store,lambda _:'自己夸自己',pause=lambda _:None,reader=reader).run()
        self.assertEqual(feed.calls,[])

    def test_forget_group_transcript_does_not_restore_native_old_messages(self):
        from conversation_memory import group_transcript
        self.record(1,'我喜欢猫',scene='room@chatroom')
        self.record(2,'忘记我喜欢猫',scene='room@chatroom')
        reader=Mock(account='self')
        reader.messages.return_value=[msg(1,'我喜欢猫',stamp=time.time()-1),msg(2,'忘记我喜欢猫'),msg(3,'你好','b')]
        job=dict(account='self',username='room@chatroom',batch=[dict(source='s',message=msg(3,'你好','b'))])
        context=group_transcript(reader,job,{'a':'甲','b':'乙'},people=self.people)
        self.assertNotIn('我喜欢猫',context)
        self.assertIn('你好',context)

    def test_memory_only_acknowledges_after_ingested_request(self):
        self.assertEqual(self.people.memory_reply('忘记我喜欢猫',account='self',person='a',stamp=time.time()),'')
        self.record(1,'我喜欢猫')
        stamp=time.time()+2
        self.record(2,'忘记我喜欢猫',stamp=stamp)
        self.assertIn('已清除',self.people.memory_reply('忘记我喜欢猫',account='self',person='a',stamp=stamp))

    def test_auto_comment_native_new_thread_receipt_confirms_exact_post(self):
        cfg=config(like_enabled=False,comment_enabled=True,dry_run=False)
        target=post('甲','猫猫在睡觉')
        reader=Mock(account='self')
        reader.contacts.return_value={'a':'甲'}
        old=dict(id='native',username='a',text=target.text,thread=[],likes=[])
        new=dict(old,thread=[dict(id='new',username='self',text='睡得真香')])
        reader.moments.side_effect=[[old],[new]]
        feed=FakeFeed([target])
        result=Runner(feed,cfg,self.store,lambda _:'睡得真香',pause=lambda _:None,reader=reader).run()
        self.assertEqual(result.commented,[target.id])
        self.assertEqual(self.store._rows('SELECT state FROM moment_activity')[0]['state'],'confirmed')


if __name__=='__main__':
    unittest.main()
