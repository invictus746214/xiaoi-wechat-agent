import tempfile,time,unittest,hashlib
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import bridge,database_service as service,settings
from event_pipeline import Inbox
from store import Store
from runtime_controls import RuntimeControls
from followups import Followups
from tasks import Tasks
from test_followups import job as followup_job,NOW as FOLLOWUP_NOW
import test_sticker_commands as sticker_test_fixtures

class SixCapabilityIntegrationTests(unittest.TestCase):
    def job(self,kind=34,text='native-audio'):
        message=dict(local_id=1,type=kind,text=text,timestamp=time.time(),sender='friend',outgoing=False,server_id='42')
        event=hashlib.sha256(b'self\0friend\0message/message_0.db\x001').hexdigest()[:32]
        row=dict(event_id=event,batch_id='batch',username='friend',title='friend',source='message/message_0.db',message=message)
        inbox=Inbox(':memory:')
        try:return service.build_job([row],inbox,bridge,settings.normalize({}),'self')
        finally:inbox.close()

    def test_voice_transcript_is_source_bound_and_enters_existing_context(self):
        job=self.job();reader=SimpleNamespace(account='self');responder=Mock(return_value='知道啦')
        cfg=settings.normalize({'voice_input':{'enabled':True},'voice':{'enabled':False}})
        with patch('voice_input.transcribe_message',return_value='明天下午我去面试') as transcribe:
            self.assertEqual(service.prepare_model(job,reader,responder,None,bridge,cfg),'知道啦')
        self.assertIn('明天下午我去面试',responder.call_args.args[0]['message'])
        supplied=transcribe.call_args.args[1]
        self.assertEqual((supplied['account'],supplied['chat_username'],supplied['source']),('self','friend','message/message_0.db'))
        self.assertNotIn('account',job['batch'][0]['message'])

    def test_unavailable_voice_transcript_never_reaches_model_as_guessed_speech(self):
        job=self.job();responder=Mock(return_value='我猜你说的是天气')
        cfg=settings.normalize({'voice_input':{'enabled':True},'voice':{'enabled':False}})
        with patch('voice_input.transcribe_message',return_value=''):
            reply=service.prepare_model(job,SimpleNamespace(account='self'),responder,None,bridge,cfg)
        self.assertIn('未能转写',reply);responder.assert_not_called()

    def test_one_failed_voice_does_not_discard_another_verified_transcript(self):
        job=self.job();job['batch'].append(dict(job['batch'][0],message=dict(job['batch'][0]['message'],local_id=2)))
        responder=Mock(return_value='好，面试加油')
        with patch('voice_input.transcribe_message',side_effect=['明天去面试','']):
            reply=service.prepare_model(job,SimpleNamespace(account='self'),responder,None,bridge,settings.normalize({'voice':{'enabled':False}}))
        self.assertEqual(reply,'好，面试加油');self.assertIn('明天去面试',responder.call_args.args[0]['message'])

    def test_group_management_request_is_rejected_as_an_operation(self):
        with closing(Store()) as store:
            job=self.job(1,'@小i 暂停朋友圈互动');job.update(kind='group',username='room@chatroom',self_names=['小i'],is_mention=True)
            reply=service.capability_reply(job,SimpleNamespace(account='self'),store,settings.normalize({}),controls=RuntimeControls(store,'self'))
            self.assertIn('主人',reply)

    def test_pause_collection_keeps_image_understanding_without_saving(self):
        job=self.job(47,'[表情]');people=Mock();people.observation.return_value='一只开心的小猫'
        cfg=settings.normalize({'stickers':{'enabled':True,'collect_enabled':False,'prepare_before_reply':False},'voice':{'enabled':False}})
        with patch('database_service.collect_sticker') as collect:
            self.assertEqual(service.prepare_model(job,SimpleNamespace(account='self'),lambda _:'看到了',None,bridge,cfg,people=people,stickers=Mock()),'看到了')
        collect.assert_not_called()

    def test_verified_owner_command_routes_and_persists_without_model(self):
        with closing(Store()) as store:
            controls=RuntimeControls(store,'self');job=self.job(1,'暂停朋友圈互动');cfg=settings.normalize({})
            reply=service.capability_reply(job,SimpleNamespace(account='self'),store,cfg,controls=controls,owner=True)
            self.assertIn('已暂停',reply)
            self.assertFalse(controls.apply(cfg)['moments']['interaction_enabled'])

    def test_nonowner_command_cannot_be_reinterpreted_as_success(self):
        with closing(Store()) as store:
            controls=RuntimeControls(store,'self');job=self.job(1,'暂停朋友圈互动');cfg=settings.normalize({})
            reply=service.capability_reply(job,SimpleNamespace(account='self'),store,cfg,controls=controls,owner=False)
            self.assertIn('主人',reply)
            self.assertTrue(controls.apply(cfg)['moments']['interaction_enabled'])

    def test_stable_management_binding_does_not_promote_other_relay_contacts(self):
        cfg=settings.normalize({'runtime':{'owner_ids':['primary']},'relay':{'allowed_titles':['old-admin']}})
        self.assertTrue(service.verified_management_owner(dict(kind='private',username='primary'),{},cfg))
        self.assertFalse(service.verified_management_owner(dict(kind='private',username='other'),{'other':'old-admin'},cfg))

    def test_strict_group_mode_overrides_engagement_and_always_reply_list(self):
        cfg=settings.normalize({'policy':{'group_strict_mention':True,'group_always_reply':['room']}})
        self.assertFalse(bridge.policy.allowed(cfg['policy'],dict(kind='group',name='room',engaged=True,is_mention=False))[0])
        self.assertTrue(bridge.policy.allowed(cfg['policy'],dict(kind='group',name='room',engaged=True,is_mention=True))[0])

    def test_followup_offer_is_context_and_explicit_consent_creates_task(self):
        with closing(Store()) as store:
            followups=Followups(store);cfg=settings.normalize({});reader=SimpleNamespace(account='self')
            proposal=self.job(1,'我明天下午3点有面试')
            self.assertIsNone(service.capability_reply(proposal,reader,store,cfg,followups=followups))
            self.assertIn('尚未安排',proposal['conversation_context'])
            self.assertEqual(store._rows('SELECT * FROM tasks'),[])
            consent=self.job(1,'好，之后问问我');consent['focus']['message']['local_id']=2
            consent['focus']['event_id']=hashlib.sha256(b'self\0friend\0message/message_0.db\x002').hexdigest()[:32]
            reply=service.capability_reply(consent,reader,store,cfg,followups=followups)
            self.assertIn('已创建任务',reply);self.assertEqual(len(store._rows('SELECT * FROM tasks')),1)

    def test_legacy_sticker_status_backfills_only_native_verified_sender(self):
        case=sticker_test_fixtures.StickerCommandTests();case.setUp();self.addCleanup(case.tearDown)
        _,row=case.add(sender=None)
        reader=Mock(account='account');reader.messages.return_value=[dict(row['message'],sender='alice')]
        reply=service.capability_reply(case.job('表情保存状态'),reader,case.store,settings.normalize({}),stickers=case.library)
        self.assertIn('原文件已保存1',reply)

    def test_continue_command_rechecks_source_before_confirming_collection(self):
        case=sticker_test_fixtures.StickerCommandTests();case.setUp();self.addCleanup(case.tearDown)
        identifier,row=case.add();job=case.job('刚才没收藏的继续');reader=Mock(account='account');reader.messages.return_value=[row['message']]
        service.capability_reply(job,reader,case.store,settings.normalize({}),stickers=case.library)
        def confirmed(*args,**kwargs):case.library.set_native(identifier,'f'*32,'confirmed');return 'confirmed'
        with patch('database_service.sync_sticker',side_effect=confirmed) as sync:
            reply=service.continue_sticker_request(None,reader,job,case.library,case.store,bridge,lambda:False)
        self.assertIn('原生收藏已确认1',reply);sync.assert_called_once()

    def test_continue_command_never_clicks_a_different_sender_source(self):
        case=sticker_test_fixtures.StickerCommandTests();case.setUp();self.addCleanup(case.tearDown)
        _,row=case.add();job=case.job('刚才没收藏的继续');reader=Mock(account='account');reader.messages.return_value=[row['message']]
        service.capability_reply(job,reader,case.store,settings.normalize({}),stickers=case.library)
        reader.messages.return_value=[dict(row['message'],sender='bob')]
        with patch('database_service.sync_sticker') as sync:
            reply=service.continue_sticker_request(None,reader,job,case.library,case.store,bridge,lambda:False)
        self.assertIn('尚未进行收藏',reply);sync.assert_not_called()

    def test_model_sees_actual_enabled_capabilities_without_action_receipts(self):
        with closing(Store()) as store:
            cfg=settings.normalize({'voice_input':{'enabled':True},'followups':{'enabled':True},'memory':{'summary_every':0}})
            respond=bridge.build_responder(cfg,store,'test-key',{'text':'人格'})
            with patch('bridge.agent.respond',return_value='我在') as model,patch('bridge.agent.maybe_summarize'):
                respond(self.job(1,'你能听语音吗'))
            self.assertIn('语音转写',model.call_args.kwargs['conversation_context'])

    def test_unaddressed_group_messages_do_not_bypass_interjection_decision(self):
        job=dict(kind='group',is_mention=False,title='room');cfg=settings.normalize({})
        self.assertFalse(service.group_reply_relevant(job,False,True,False,cfg))
        self.assertTrue(service.group_reply_relevant(job,False,True,True,cfg))
        cfg['policy']['group_always_reply']=['room']
        self.assertFalse(service.group_reply_relevant(job,False,False,False,cfg))

    def test_group_media_continues_recent_same_speaker_turn_without_new_at(self):
        from conversation_memory import group_turn
        job=self.job();job.update(kind='group',username='room',is_mention=False,focus_text='[语音]',sender_names={})
        policy=settings.normalize({})['policy'];engaged={('room','friend'):1000+policy['group_engaged_seconds']}
        self.assertEqual(group_turn(job,'self',policy,bridge,engaged,{},1010),(True,False))
        self.assertEqual(group_turn(job,'self',policy,bridge,engaged,{},1040),(False,True))

    def test_media_payload_at_token_is_not_a_real_group_mention(self):
        job=self.job(47,'@小i opaque-media');batch=job['batch'];batch[0]['username']='room@chatroom'
        inbox=Inbox(':memory:')
        try:
            actual=service.build_job(batch,inbox,bridge,settings.normalize({'policy':{'group_self_name':'小i'}}),'self')
        finally:inbox.close()
        self.assertFalse(actual['is_mention'])

    def test_persisted_followup_uses_natural_question_in_existing_delivery(self):
        with closing(Store()) as store:
            followups=Followups(store);followups.handle(followup_job('明天下午3点面试结束后问问我进展'),FOLLOWUP_NOW)
            cfg=settings.normalize({'delivery':{'quota_enabled':False}});reader=SimpleNamespace(account='bot')
            inbox=Mock();inbox.count.return_value=0
            with patch('database_service.send_reply',return_value='done') as send:
                self.assertTrue(service.run_due_task(None,reader,Tasks(store),store,inbox,bridge,cfg,now=FOLLOWUP_NOW+31*3600+1,followups=followups))
            self.assertEqual(send.call_args.args[3],'之前提到的面试进展怎么样？')
            self.assertEqual(store._rows('SELECT state FROM tasks')[0]['state'],'confirmed')

if __name__=='__main__':unittest.main()
