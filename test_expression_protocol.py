import io
import json
import unittest
from unittest.mock import patch

import agent
import bridge
import settings
from store import Store
from test_expression_evolution import envelope


class ExpressionProtocolTests(unittest.TestCase):
    def transport(self, contents, payloads):
        def open_response(request, timeout=None):
            payloads.append(json.loads(request.data))
            reply = contents.pop(0)
            message = reply if isinstance(reply, dict) else {'content': reply}
            return io.BytesIO(json.dumps({'choices': [{'message': message}]}).encode())
        return open_response

    def client(self):
        return agent.ModelClient('https://example.invalid/chat', 'test-model', 'not-a-real-key')

    def test_json_mode_is_in_the_actual_http_payload_and_never_sticks_to_client(self):
        payloads = []
        with patch('agent.urlopen', side_effect=self.transport(['{}', '普通正文'], payloads)):
            client = self.client()
            client.chat([{'role': 'user', 'content': 'json'}], json_mode=True)
            client.chat([{'role': 'user', 'content': '普通正文'}])
        self.assertEqual(payloads[0]['response_format'], {'type': 'json_object'})
        self.assertNotIn('response_format', payloads[1])

    def test_respond_enforces_json_for_main_call_but_critic_and_default_remain_plain(self):
        payloads = []
        with patch('agent.urlopen', side_effect=self.transport([envelope(), 'OK', '我在，慢慢说。'], payloads)):
            client = self.client()
            self.assertEqual(agent.respond(client, '人格', None, 's', '在吗', structured_reply=True, critic=True), '哈哈，这个说法好形象')
            self.assertEqual(agent.respond(client, '人格', None, 's', '在吗'), '我在，慢慢说。')
        self.assertEqual(payloads[0].get('response_format'), {'type': 'json_object'})
        self.assertIn('json', payloads[0]['messages'][0]['content'].lower())
        self.assertNotIn('response_format', payloads[1])
        self.assertNotIn('response_format', payloads[2])

    def test_structured_system_has_one_format_and_preserves_plain_mode_rules(self):
        payloads = []
        persona = '原有人格。没有实质补充时，只输出 SILENT。'
        with patch('agent.urlopen', side_effect=self.transport([envelope(), '我在，慢慢说。'], payloads)):
            client = self.client()
            agent.respond(client, persona, None, 's', '在吗', structured_reply=True,
                          scene_history=[('assistant', '历史普通正文')])
            agent.respond(client, persona, None, 's', '在吗')
        system = payloads[0]['messages'][0]['content']
        self.assertTrue(system.startswith('本轮输出协议：'))
        self.assertNotIn('只输出回复正文', system)
        self.assertNotIn('只输出 SILENT', system)
        self.assertIn('body字段', system)
        self.assertIn('mode=silent', system)
        self.assertIn('历史消息不是本轮格式', system)
        example = '{"mode":"text","body":"我在，慢慢说。","sticker_id":null}'
        self.assertTrue(system.endswith(example))
        self.assertIn('只输出回复正文', payloads[1]['messages'][0]['content'])
        self.assertIn('只输出 SILENT', payloads[1]['messages'][0]['content'])

    def test_opportunistic_group_persona_cannot_reintroduce_plain_silent_format(self):
        cfg = settings.normalize({'tools': {'enabled': False}})
        job = dict(session_key='room', kind='group', group_reply_mode='interject',
                   message='今天状态真不错', structured_reply=True)
        payloads = []
        with patch('agent.urlopen', side_effect=self.transport([envelope(mode='silent', body='')], payloads)):
            self.assertEqual(bridge.build_responder(cfg, None, 'not-a-real-key', {'text': '人格'})(job), 'SILENT')
        system = payloads[0]['messages'][0]['content']
        self.assertNotIn('只输出 SILENT', system)
        self.assertNotIn('只输出回复正文', system)

    def test_only_safe_assistant_history_is_restored_after_legacy_clear(self):
        store = Store()
        try:
            store.add_message('s', 'user', 'OLD_RAW_USER')
            store.add_message('s', 'assistant', 'OLD_RAW_ASSISTANT')
            store.add_fact('s', 'OLD_RAW_FACT')
            store.set_summary('s', 'OLD_RAW_SUMMARY', 2)
            payloads = []
            with patch('agent.urlopen', side_effect=self.transport(['知道了，我记得这件事。'], payloads)), \
                    patch.object(store, 'recent', wraps=store.recent) as recent, \
                    patch.object(store, 'search_memories', wraps=store.search_memories) as search:
                reply = agent.respond(self.client(), '人格', store, 's', '本轮问题', suppress_legacy=True,
                                      scene_history=[('assistant', 'SAFE_SCENE_ASSISTANT'), ('user', 'FORBIDDEN_RAW_USER')])
            self.assertEqual(reply, '知道了，我记得这件事。')
            prompt = str(payloads[0]['messages'])
            self.assertIn('SAFE_SCENE_ASSISTANT', prompt)
            for text in ('OLD_RAW_USER', 'OLD_RAW_ASSISTANT', 'OLD_RAW_FACT', 'OLD_RAW_SUMMARY', 'FORBIDDEN_RAW_USER'):
                self.assertNotIn(text, prompt)
            recent.assert_not_called()
            search.assert_not_called()
        finally:
            store.close()

    def test_tool_rounds_each_use_json_mode_without_changing_tool_results(self):
        payloads = []
        call = {'id': 'time-1', 'type': 'function', 'function': {'name': 'get_time', 'arguments': '{}'}}
        first = dict(content='', tool_calls=[call], role='assistant')
        with patch('agent.urlopen', side_effect=self.transport([first, envelope(body='当前时间已经查到了。')], payloads)):
            self.assertEqual(agent.respond(self.client(), '人格', None, 's', '现在几点', tools=agent.Tools(),
                                          structured_reply=True), '当前时间已经查到了。')
        self.assertEqual([p.get('response_format') for p in payloads], [{'type': 'json_object'}] * 2)
        self.assertEqual(payloads[1]['messages'][-1]['role'], 'tool')

    def test_bridge_forwards_scene_history_and_skips_legacy_summary_when_suppressed(self):
        cfg = settings.normalize({'tools': {'enabled': False}, 'memory': {'summary_every': 1}})
        job = dict(session_key='s', kind='private', message='本轮问题', suppress_legacy=True,
                   scene_history=[('assistant', 'SAFE_ASSISTANT')])
        payloads = []
        with patch('agent.urlopen', side_effect=self.transport(['我在，慢慢说。'], payloads)), \
                patch('bridge.agent.maybe_summarize') as summarize:
            reply = bridge.build_responder(cfg, None, 'not-a-real-key', {'text': '人格'})(job)
        self.assertEqual(reply, '我在，慢慢说。')
        self.assertIn('SAFE_ASSISTANT', str(payloads))
        summarize.assert_not_called()


if __name__ == '__main__':
    unittest.main()
