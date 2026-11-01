import unittest
from unittest.mock import Mock,patch
import settings,bridge,database_service as service
import agent
from reply_plan import freeze_reply,restore_reply
from test_goal_intelligence import Client,result,reply


class IntelligenceIntegrationTests(unittest.TestCase):
    def test_group_self_mention_forget_is_normalized_without_touching_other_mentions(self):
        cfg=settings.normalize({'policy':{'group_self_name':'小i'}})
        rows=[dict(sender='a',type=1,outgoing=False,text='@小i 忘记关于我的所有记忆'),dict(sender='a',type=1,outgoing=False,text='@别人 忘记关于我的所有记忆')]
        normalized=service.normalize_forget_messages(rows,bridge,cfg)
        self.assertEqual(normalized[0]['text'],'忘记关于我的所有记忆')
        self.assertEqual(normalized[1]['text'],rows[1]['text'])
        self.assertEqual(rows[0]['text'],'@小i 忘记关于我的所有记忆')

    def test_persistent_no_sticker_preference_removes_candidates(self):
        cfg=settings.normalize({'learning':{'enabled':True},'quality':{'structured_reply':True},'stickers':{'enabled':True,'auto_send':True}})
        job=dict(kind='private',event_id='e',username='a',message='哈哈',voice_mode=False,force_reply=False,
                 batch=[dict(message={'type':1,'text':'哈哈'},reply=None)])
        feedback=Mock();feedback.context.return_value='';feedback.preferences.return_value={'emoji':'none'}
        library=Mock()
        service.prepare_model(job,None,lambda j:'确实好笑',None,bridge,cfg,stickers=library,feedback=feedback)
        self.assertEqual(job['sticker_candidates'],[]);library.candidates.assert_not_called()

    def test_complex_model_failure_returns_honest_fallback_instead_of_retrying_silently(self):
        client=Mock();client.max_tokens=800;client.chat.side_effect=agent.ModelError('request_failed')
        value=agent.respond(client,'人格',None,'s','请详细分析',structured_reply=True,intelligence_mode='deliberate')
        self.assertIn('尚未核实',value)
        self.assertNotIn('已创建',value)
        self.assertEqual(client.chat.call_count,1)

    def test_verified_style_enums_override_default_habits_without_raw_instructions(self):
        client=Client([result(reply('那就安心歇会儿。'))])
        value=agent.respond(client,'默认人格喜欢追问',None,'s','今天终于休息了',structured_reply=True,
                            reply_preferences={'followups':'necessary','length':'short','ignored':'test_bad_override_9988'})
        system=client.calls[0][0][0]['content']
        self.assertIn('优先于默认人格的表达习惯',system)
        self.assertNotIn('test_bad_override_9988',system)
        self.assertEqual(value,'那就安心歇会儿。')

    def test_time_tool_uses_shanghai_even_when_host_localtime_differs(self):
        with patch('agent.time.time',return_value=1793548800),patch('agent.time.localtime',side_effect=AssertionError('host timezone must not set user date')):
            value=agent._time_tool({})
        self.assertIn('+08:00',value)

    def test_queued_semantic_sources_survive_restart_and_are_rechecked(self):
        job=dict(structured_reply=True,sticker_choice=None,sticker_only=False,semantic_sources=['a'*64],account='self',person_id='a',username='a',focus_text='上次呢')
        restored={};restore_reply(restored,freeze_reply(job,'周六在图书馆'))
        self.assertEqual(restored['semantic_sources'],['a'*64])
        restored.update({key:job[key] for key in ('account','person_id','username','focus_text')})
        people=Mock();people.semantic_candidates.return_value=[]
        self.assertFalse(service.semantic_reply_current(restored,people))
        people.semantic_candidates.return_value=[{'id':'a'*64}]
        self.assertTrue(service.semantic_reply_current(restored,people))

    def test_feedback_and_semantic_context_are_worker_inputs_not_send_commands(self):
        cfg=settings.normalize({'learning':{'enabled':True},'memory':{'semantic_enabled':True},'quality':{'intelligence_enabled':True}})
        job=dict(kind='group',event_id='e',group_focus='当前发言人甲',conversation_context='原有来源',message='上次那个安排呢',focus_text='上次那个安排呢')
        feedback=Mock();feedback.context.return_value='本人要求少反问'
        selector=Mock(return_value='[已验证本群来源] 周六图书馆')
        service.enrich_intelligence(job,cfg,Mock(),feedback,selector)
        self.assertTrue(job['intelligence_enabled']);self.assertIn('少反问',job['feedback_context'])
        self.assertIn('周六图书馆',job['group_focus']);self.assertIn('周六图书馆',job['conversation_context'])
        feedback.observe.assert_called_once_with(job)

    def test_semantic_failure_keeps_existing_scene_memory_available(self):
        cfg=settings.normalize({'memory':{'semantic_enabled':True}})
        job=dict(kind='private',event_id='e',conversation_context='有效场景原话')
        service.enrich_intelligence(job,cfg,Mock(),None,Mock(side_effect=TimeoutError))
        self.assertEqual(job['conversation_context'],'有效场景原话')

    def test_task_handler_preserves_native_job_and_skips_direct_parser_for_composite(self):
        cfg=settings.normalize({'tasks':{'enabled':True}});tasks=Mock()
        job=dict(event_id='e',focus_text='查明天深圳天气，明天上午8点提醒我带伞')
        with patch('tasks.is_composite_request',return_value=True):
            self.assertIsNone(service.task_command_reply(tasks,job))
        tasks.handle.assert_not_called()
        job['task_handler']('明天上午8点提醒我带伞')
        self.assertIs(tasks.create_from_tool.call_args.args[0],job)
        self.assertEqual(tasks.create_from_tool.call_args.args[1],'明天上午8点提醒我带伞')

if __name__=='__main__':unittest.main()
