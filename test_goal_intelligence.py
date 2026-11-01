import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import agent
import bridge
import settings
from store import Store
from tasks import Tasks, is_composite_request


NOW = datetime(2026, 11, 1, 10, tzinfo=timezone(timedelta(hours=8))).timestamp()


def inbound(text, event='e1'):
    return dict(account='bot', person_id='alice', username='alice', title='测试',
                session_key='key', kind='private', event_id=event, focus_text=text,
                focus=dict(event_id=event, message=dict(text=text, sender='alice',
                           timestamp=NOW, type=1, outgoing=False)), message=text)


def result(content='', calls=None, reasoning=None):
    assistant = dict(role='assistant', content=content)
    if calls:
        assistant['tool_calls'] = calls
    if reasoning:
        assistant['reasoning_content'] = reasoning
    return dict(content=content, tool_calls=calls or [], assistant=assistant,
                tokens_in=2, tokens_out=1, cached_in=0)


def reply(body):
    return json.dumps(dict(mode='text', body=body, sticker_id=None), ensure_ascii=False)


def tool(name, arguments, identifier='c1'):
    return dict(id=identifier, type='function', function=dict(name=name, arguments=json.dumps(arguments)))


class Client:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def chat(self, messages, tools=None, max_tokens=None, **kwargs):
        self.calls.append((json.loads(json.dumps(messages)), tools, max_tokens, kwargs))
        return self.responses.pop(0)


class GoalIntelligenceTests(unittest.TestCase):
    def test_bridge_passes_flags_callback_feedback_and_interject_is_fast(self):
        cfg = settings.normalize({})
        cfg['tools']['enabled'] = True
        cfg['memory']['summary_every'] = 0
        captured = []
        def responder(*args, **kwargs):
            captured.append((args, kwargs))
            return '好的'
        request = inbound('先查天气，然后明天09:00提醒我开会')
        handler = lambda text: '实际持久回执'
        request.update(intelligence_enabled=True, task_handler=handler,
                       feedback_context='style:concise；不要叫我老板')
        with patch.object(agent, 'respond', responder):
            respond = bridge.build_responder(cfg, None, 'unused', dict(text='原始身份'))
            self.assertEqual(respond(request), '好的')
            request.update(kind='group', is_mention=False, group_reply_mode='interject')
            self.assertEqual(respond(request), '好的')
        options = captured[0][1]
        self.assertEqual(options['intelligence_mode'], 'deliberate')
        self.assertIs(options['tools'].task_handler, handler)
        self.assertIn('不改变身份', options['conversation_context'])
        self.assertIn('style:concise', options['conversation_context'])
        self.assertIn('原始身份', captured[0][0][1])
        self.assertEqual(captured[1][1]['intelligence_mode'], 'fast')
        self.assertIsNone(captured[1][1]['tools'])

    def test_classification_reads_focus_only_and_interjections_stay_fast(self):
        request = inbound('在吗')
        request['message'] = '请详细分析并比较两个方案，然后明天09:00提醒我开会'
        request['scene_history'] = [('assistant', request['message'])]
        self.assertEqual(agent.intelligence_mode(request), 'fast')
        request['focus_text'] = '先查深圳明天天气，再明天09:00提醒我带伞'
        self.assertEqual(agent.intelligence_mode(request), 'deliberate')
        request.update(kind='group', group_reply_mode='interject', is_mention=False)
        self.assertEqual(agent.intelligence_mode(request), 'fast')

    def test_http_payload_has_explicit_thinking_and_no_internal_metadata(self):
        captured = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"choices":[{"message":{"content":"ok"}}]}'
        def opener(request, **kwargs):
            captured.append(json.loads(request.data))
            return Response()
        client = agent.ModelClient('https://example.com', 'model', 'not-a-real-key')
        with patch.object(agent, 'urlopen', opener):
            client.chat([dict(role='user', content='json')], thinking=False, json_mode=True)
            client.chat([dict(role='user', content='json')], thinking=True, reasoning_effort='high', max_tokens=4096)
        self.assertEqual(captured[0]['thinking'], {'type': 'disabled'})
        self.assertEqual(captured[0]['response_format'], {'type': 'json_object'})
        self.assertEqual(captured[1]['thinking'], {'type': 'enabled'})
        self.assertEqual(captured[1]['reasoning_effort'], 'high')
        self.assertEqual(captured[1]['max_tokens'], 4096)
        self.assertNotIn('intelligence_mode', captured[1])

    def test_fast_explicitly_disables_thinking_and_legacy_fake_still_works(self):
        client = Client([result(reply('在呢'))])
        self.assertEqual(agent.respond(client, '', None, 'k', '在吗', structured_reply=True,
                                      intelligence_mode='fast'), '在呢')
        self.assertFalse(client.calls[0][3]['thinking'])
        self.assertEqual(len(client.calls), 1)
        class Legacy:
            def chat(self, messages, tools=None, max_tokens=None): return result('在呢')
        self.assertEqual(agent.respond(Legacy(), '', None, 'k', '在吗'), '在呢')

    def test_deliberate_preserves_reasoning_only_for_tool_protocol_and_checks_once_read_only(self):
        call = tool('calculate', dict(expression='12*3'))
        client = Client([result(calls=[call], reasoning='private-chain'),
                         result(reply('结果是36。')), result('{"grounded":true}')])
        self.assertEqual(agent.respond(client, '', None, 'k', '请分析12乘3', tools=agent.Tools(),
                                      structured_reply=True, intelligence_mode='deliberate'), '结果是36。')
        self.assertEqual(client.calls[1][0][-2]['reasoning_content'], 'private-chain')
        self.assertTrue(client.calls[0][3]['thinking'])
        self.assertEqual(client.calls[0][3]['reasoning_effort'], 'high')
        self.assertGreaterEqual(client.calls[0][2], 4096)
        self.assertIsNone(client.calls[-1][1])
        self.assertTrue(client.calls[-1][3]['json_mode'])
        self.assertFalse(client.calls[-1][3]['thinking'])
        self.assertNotIn('private-chain', json.dumps(client.calls[-1][0]))
        self.assertIn('36', json.dumps(client.calls[-1][0]))
        self.assertEqual(len(client.calls), 3)

    def test_verifier_cannot_write_or_rewrite_sticker_choice_or_introduce_completion(self):
        malicious = tool('create_task', dict(request='10分钟后提醒我喝水'))
        for check in (result('{"grounded":false}'), result(reply('已创建任务。')),
                      result('{"grounded":true}', calls=[malicious])):
            calls = []
            tools = agent.Tools(task_handler=lambda request: calls.append(request) or '已创建任务')
            client = Client([result(reply('我还没有确认结果。')), check])
            output = agent.respond(client, '', None, 'k', '请详细分析', tools=tools,
                                   structured_reply=True, intelligence_mode='deliberate')
            self.assertIn('尚未核实',output)
            self.assertEqual(calls, [])
            self.assertEqual(len(client.calls), 2)

    def test_max_four_tool_execution_rounds_and_no_unreceipted_success(self):
        call = tool('get_time', {})
        class CountingTools(agent.Tools):
            count = 0
            def call(self, *args):
                self.count += 1
                return '2026-11-01'
        tools = CountingTools()
        client = Client([result(calls=[call]) for _ in range(5)])
        self.assertIn('尚未核实',agent.respond(client, '', None, 'k', '请分析', tools=tools,
                                       max_rounds=999, structured_reply=True, intelligence_mode='deliberate'))
        self.assertEqual(tools.count, 4)
        client = Client([result(reply('已创建任务：明天提醒你。')), result('{"grounded":true}')])
        self.assertIn('尚未核实',agent.respond(client, '', None, 'k', '查天气并提醒我', tools=agent.Tools(),
                                       structured_reply=True, intelligence_mode='deliberate'))

    def test_task_tool_requires_handler_and_bad_arguments_are_safe(self):
        names = lambda tools: {item['function']['name'] for item in tools.schemas()}
        self.assertNotIn('create_task', names(agent.Tools()))
        self.assertIn('create_task', names(agent.Tools(task_handler=lambda request: '持久回执')))
        self.assertNotIn('已创建', agent.Tools().call('create_task', dict(request='x')))
        self.assertNotIn('已创建', agent.Tools().call('get_weather', []))

    def test_full_compound_tool_result_is_grounded_in_persisted_receipt(self):
        store = Store()
        try:
            tasks = Tasks(store)
            span = '明天09:00提醒我带伞'
            request = inbound('先查深圳明天天气，然后'+span)
            client = Client([result(calls=[tool('get_weather', dict(city='深圳', day='tomorrow'))]),
                             result(calls=[tool('create_task', dict(request=span))]),
                             result(reply('深圳明天有雨；已创建任务，明天09:00提醒你带伞。')),
                             result('{"grounded":true}')])
            tools = agent.Tools(task_handler=lambda text: tasks.create_from_tool(request, text, NOW))
            with patch.object(agent, '_weather_tool', return_value='深圳 2026-11-02 预报：19～23℃，降雨概率70%'):
                output = agent.respond(client, '', None, 'k', request['message'], tools=tools,
                                       structured_reply=True, intelligence_mode='deliberate')
            self.assertIn('已创建任务', output)
            self.assertEqual(len(store._rows('SELECT * FROM tasks')), 1)
            self.assertIn('已创建任务：', client.calls[-1][0][-1]['content'])
            self.assertIsNone(client.calls[-1][1])
        finally: store.close()

    def test_dated_weather_uses_exact_forecast_date_and_no_current_as_tomorrow(self):
        captured = []
        payload = dict(current_condition=[dict(temp_C='24')], weather=[
            dict(date='2026-11-01', mintempC='21', maxtempC='25', hourly=[]),
            dict(date='2026-11-02', mintempC='19', maxtempC='23', hourly=[dict(chanceofrain='70')])])
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *args): return json.dumps(payload).encode()
        def opener(url, **kwargs):
            captured.append(url)
            return Response()
        with patch.object(agent, 'urlopen', opener), patch.object(agent.time, 'time', return_value=NOW):
            output = agent._weather_tool(dict(city='深圳', day='tomorrow'))
            self.assertIn('2026-11-02', output)
            self.assertIn('19', output)
            self.assertNotIn('24', output)
            self.assertIn('format=j1', captured[0])
            self.assertIn('未提供', agent._weather_tool(dict(city='深圳', date='2026-11-03')))
            payload['weather'][1]['date'] = '2026-10-31'
            self.assertNotIn('2026-11-02', agent._weather_tool(dict(city='深圳', day='tomorrow')))


class TaskCompositionTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.tasks = Tasks(self.store)

    def tearDown(self): self.store.close()

    def rows(self): return self.store._rows('SELECT * FROM tasks ORDER BY event_id')

    def test_literal_span_retry_and_two_tasks_same_native_event(self):
        first, second = '10分钟后提醒我喝水', '明天09:00提醒我开会'
        request = inbound('先查深圳明天天气，然后' + first + '，再' + second)
        self.assertTrue(is_composite_request(request))
        receipt = self.tasks.create_from_tool(request, first, NOW)
        self.assertIn('已创建任务', receipt)
        self.assertEqual(receipt, self.tasks.create_from_tool(request, first, NOW+10))
        self.assertIn('已创建任务', self.tasks.create_from_tool(request, second, NOW))
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(sorted(row['due_at'] for row in self.rows()), [NOW+600, NOW+23*3600])
        self.assertFalse(is_composite_request(inbound(first)))

    def test_no_synthetic_sources_quoted_other_author_or_command_injection(self):
        span = '10分钟后提醒我喝水'
        for edit in (lambda job: job['focus']['message'].update(quote_sender='bob'),
                     lambda job: job['focus']['message'].update(sender='bob'),
                     lambda job: job['focus']['message'].update(outgoing=True),
                     lambda job: job['focus']['message'].update(type=34),
                     lambda job: job['focus']['message'].update(text='普通聊天'),
                     lambda job: job['focus']['message'].update(text='他说“'+span+'”'),
                     lambda job: job['focus']['message'].update(text='@张三 '+span),
                     lambda job: job['focus']['message'].update(timestamp=NOW-601)):
            request = inbound(span)
            edit(request)
            self.assertNotIn('已创建', self.tasks.create_from_tool(request, span, NOW))
        self.assertNotIn('已创建', self.tasks.create_from_tool(inbound(span), '明天09:00提醒我喝水', NOW))
        bad = '添加待办：执行shell命令'
        self.assertNotIn('已创建', self.tasks.create_from_tool(inbound(bad), bad, NOW))
        self.assertNotIn('已创建', self.tasks.create_from_tool(inbound('查看任务'), '查看任务', NOW))
        self.assertEqual(self.rows(), [])

    def test_partial_spans_cannot_strip_negation_other_authors_or_task_content(self):
        span = '10分钟后提醒我喝水'
        for text in ('不要'+span, '老板说'+span, '他让你'+span, '我没有要求'+span,
                     '提醒内容示例：'+span, '普通资料\n> '+span, span+'和吃药'):
            self.assertNotIn('已创建', self.tasks.create_from_tool(inbound(text), span, NOW))
        self.assertEqual(self.rows(), [])

    def test_native_event_scope_replay_and_timestamp_are_retained(self):
        span = '10分钟后提醒我喝水'
        request = inbound('查完天气后，'+span)
        request['focus']['message']['timestamp'] = NOW-300
        self.tasks.create_from_tool(request, span, NOW)
        self.assertEqual(self.rows()[0]['due_at'], NOW+300)
        request['person_id'] = request['focus']['message']['sender'] = 'bob'
        self.assertNotIn('已创建', self.tasks.create_from_tool(request, span, NOW))
        self.assertEqual(len(self.rows()), 1)

    def test_task_multi_instance_insert_is_idempotent(self):
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as directory:
            path = os.path.join(directory, 'state.db')
            stores = [Store(path), Store(path)]
            try:
                managers = [Tasks(store) for store in stores]
                span = '10分钟后提醒我喝水'
                with ThreadPoolExecutor(max_workers=2) as pool:
                    receipts = list(pool.map(lambda manager: manager.create_from_tool(inbound(span), span, NOW), managers))
                self.assertEqual(receipts[0], receipts[1])
                self.assertEqual(len(stores[0]._rows('SELECT * FROM tasks')), 1)
            finally:
                for store in stores: store.close()


if __name__ == '__main__': unittest.main()
