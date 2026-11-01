import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

import agent
import bridge
import settings
from sticker_library import StickerLibrary
from store import Store
from test_agent import FakeClient as PlainFakeClient
from test_sticker_library import png


IDENTIFIER = 'a' * 64
CANDIDATES = [{'id': IDENTIFIER, 'description': '小猫开心大笑', 'tags': ['开心']}]


class FakeClient(PlainFakeClient):
    def chat(self, messages, tools=None, max_tokens=None, json_mode=False):
        return super().chat(messages, tools, max_tokens)


def output(content):
    return dict(content=content, tool_calls=[], assistant={}, tokens_in=10, tokens_out=5)


def envelope(mode='text', body='哈哈，这个说法好形象', sticker_id=None, **extra):
    return json.dumps(dict(mode=mode, body=body, sticker_id=sticker_id, **extra), ensure_ascii=False)


class ExpressionReplyTests(unittest.TestCase):
    def answer(self, raw, **kwargs):
        decision = {}
        client = FakeClient([output(raw)])
        reply = agent.respond(client, '原有人格', None, 's', '今天心情真不错',
                              structured_reply=True, sticker_candidates=CANDIDATES,
                              reply_decision=decision, **kwargs)
        return reply, decision, client

    def test_body_is_parsed_before_sanitize_and_only_public_candidates_reach_model(self):
        raw = envelope(sticker_id=IDENTIFIER, reason='与当前轻松的语境合适' * 24)
        self.assertGreater(len(raw), 300)
        reply, decision, client = self.answer(raw)
        self.assertEqual(reply, '哈哈，这个说法好形象')
        self.assertEqual(decision, {'sticker_choice': IDENTIFIER, 'sticker_only': False})
        self.assertEqual(len(client.calls), 1)
        self.assertIn('sticker_only', client.calls[0][0]['content'])

    def test_invalid_structures_and_hidden_metadata_never_become_reply_text(self):
        for raw in ('普通正文', '{"mode":"text"', '```json\n' + envelope() + '\n```',
                    envelope(body=IDENTIFIER), envelope(body='{"mode":"silent"}'),
                    envelope(sticker_id='../../private'), envelope(unexpected='metadata'),
                    envelope(body='SILENT'), envelope(mode='silent', body='内部联系人', sticker_id=None),
                    '{"mode":"text","mode":"silent","body":"","sticker_id":null}'):
            with self.subTest(raw=raw):
                reply, decision, _ = self.answer(raw)
                self.assertIsNone(reply)
                self.assertEqual(decision, {})

    def test_sticker_only_requires_a_valid_id_and_silent_requires_group_scene(self):
        reply, decision, _ = self.answer(envelope(mode='sticker_only', body='', sticker_id=IDENTIFIER))
        self.assertEqual(reply, agent.STICKER_ONLY)
        self.assertTrue(decision['sticker_only'])
        for raw in (envelope(mode='sticker_only', body=''), envelope(mode='sticker_only', body='不要显示', sticker_id=IDENTIFIER), envelope(mode='silent', body='')):
            self.assertIsNone(self.answer(raw)[0])
        reply, decision, _ = self.answer(envelope(mode='silent', body=''), scene_kind='group')
        self.assertEqual(reply, 'SILENT')
        self.assertEqual(decision, {'sticker_choice': None, 'sticker_only': False})

    def test_critic_sees_only_body_and_cannot_leak_private_contract(self):
        client = FakeClient([output(envelope(sticker_id=IDENTIFIER)), output(envelope(body='隐藏结果'))])
        decision = {}
        reply = agent.respond(client, '原有人格', None, 's', '今天心情真不错', critic=True,
                              structured_reply=True, sticker_candidates=CANDIDATES, reply_decision=decision)
        self.assertIsNone(reply)
        self.assertNotIn(IDENTIFIER, client.calls[1][-1]['content'])
        self.assertEqual(decision, {})

    def test_natural_style_preserves_persona_and_explicit_detail_request(self):
        prompt = agent.build_messages('原有人格：喜欢简练', [], [], '请详细解释步骤', 'now')[0]['content']
        for phrase in ('原有人格：喜欢简练', '明确要求详细', '客服', '追问', '亲身经历'):
            self.assertIn(phrase, prompt)
        long_body = '先核对尺寸和材料，再确认配合间隙。' * 18
        client = FakeClient([output(envelope(body=long_body))])
        self.assertEqual(agent.respond(client, '原有人格', None, 's', '请详细解释步骤', structured_reply=True), long_body)

    def test_plain_default_contract_and_tools_remain_compatible(self):
        client = FakeClient([output('好呀，听起来挺有意思')])
        self.assertEqual(agent.respond(client, '原有人格', None, 's', '在吗'), '好呀，听起来挺有意思')
        self.assertNotIn('sticker_id', client.calls[0][0]['content'])
        call = {'id': 'time-1', 'function': {'name': 'get_time', 'arguments': '{}'}}
        client = FakeClient([dict(output(''), tool_calls=[call]), output(envelope(body='我查了当前时间。'))])
        self.assertEqual(agent.respond(client, '人格', None, 's', '现在几点', tools=agent.Tools(),
                                      structured_reply=True), '我查了当前时间。')
        self.assertEqual(client.calls[1][-1]['role'], 'tool')

    def test_empty_candidates_still_allow_text_and_do_not_allow_fabricated_stickers(self):
        client = FakeClient([output(envelope(body='我在，慢慢说。'))])
        self.assertEqual(agent.respond(client, '人格', None, 's', '在吗', structured_reply=True), '我在，慢慢说。')
        client = FakeClient([output(envelope(mode='sticker_only', body='', sticker_id=IDENTIFIER))])
        self.assertIsNone(agent.respond(client, '人格', None, 's', '在吗', structured_reply=True))

    def test_bridge_puts_validated_decision_on_job_but_keeps_return_type(self):
        cfg = settings.normalize({})
        cfg['tools']['enabled'] = False
        cfg['quality']['critic_enabled'] = False
        cfg['memory']['summary_every'] = 0
        job = dict(session_key='s', kind='private', message='笑死我了', structured_reply=True,
                   sticker_candidates=[dict(CANDIDATES[0], path='/private/file', native_md5='SECRET')])
        client = FakeClient([output(envelope(mode='sticker_only', body='', sticker_id=IDENTIFIER))])
        with patch('bridge.agent.ModelClient', return_value=client):
            reply = bridge.build_responder(cfg, None, 'unused', {'text': '原有人格'})(job)
        self.assertEqual(reply, agent.STICKER_ONLY)
        self.assertEqual(job['sticker_choice'], IDENTIFIER)
        self.assertTrue(job['sticker_only'])
        self.assertNotIn('/private/file', str(client.calls))
        self.assertNotIn('SECRET', str(client.calls))


class SemanticCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store()
        self.library = StickerLibrary(self.store, self.temp.name, 'account')
        raw = png()
        self.identifier = self.library.collect(raw, [hashlib.md5(raw).hexdigest()], '小猫开心大笑')
        self.library.set_native(self.identifier, 'f' * 32, 'confirmed')
        self.frequency = dict(send_probability=1, min_interval_seconds=300, same_sticker_interval_seconds=1800)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def candidates(self, text='今天状态不错', event='event', explicit=False):
        return self.library.candidates(text, 'scene', event, explicit=explicit, frequency=self.frequency)

    def test_semantic_candidates_do_not_need_keyword_overlap_and_expose_no_files_or_sources(self):
        candidates = self.candidates()
        self.assertEqual(candidates, [{'id': self.identifier, 'description': '小猫开心大笑', 'tags': ['开心']}])
        self.assertEqual(self.library.choose('今天状态不错', '这个说法很形象', 'scene', 'event',
                                           frequency=self.frequency, semantic_id=self.identifier)['id'], self.identifier)
        self.assertIsNone(self.library.choose('今天状态不错', '这个说法很形象', 'scene', 'event', frequency=self.frequency))
        self.assertIsNone(self.library.choose('哈哈', '开心', 'scene', 'event', explicit=True, semantic_id='not-in-library'))

    def test_serious_forbidden_frequency_cooldowns_and_unknown_are_not_bypassed(self):
        for text in ('医院要做手术', '不要再发表情', '密码是多少'):
            self.assertEqual(self.candidates(text, explicit=True), [])
        self.assertEqual(self.library.candidates('哈哈', 'scene', 'event', frequency={'send_probability': 0}), [])
        self.library.record_send(self.identifier, 'scene', 'old', 'confirmed')
        self.assertEqual(self.candidates(), [])
        self.assertIsNone(self.library.choose('哈哈', '开心', 'scene', 'event', frequency=self.frequency, semantic_id=self.identifier))
        self.store._write('UPDATE sticker_usage SET ts=ts-2000')
        self.assertEqual(len(self.candidates()), 1)
        self.store._write("UPDATE sticker_usage SET state='unconfirmed'")
        self.assertEqual(self.candidates(), [])
        self.assertEqual(self.candidates('发个表情', explicit=True), [])
        self.assertEqual(len(self.candidates('再发个表情', explicit=True)), 1)
        self.assertEqual(self.candidates('再发个表情', event='old', explicit=True), [])

    def test_candidates_are_bounded_and_exclude_non_native_and_damaged_tags(self):
        self.library.set_native(self.identifier, '', 'pending')
        self.assertEqual(self.candidates(), [])
        self.library.set_native(self.identifier, 'f' * 32, 'confirmed')
        original = self.library.get(self.identifier)
        for n in range(16):
            row = dict(original, id=hashlib.sha256(str(n).encode()).hexdigest())
            self.store._write('INSERT INTO sticker_assets VALUES(?,?,?,?,?,?,?,?,?,?)', tuple(row[k] for k in ('account','id','md5','path','description','tags','bytes','native_md5','native_state','created')))
        self.assertEqual(len(self.candidates()), 12)
        self.store._write('UPDATE sticker_assets SET tags=?', ('not-json',))
        self.assertEqual(self.candidates(), [])
        self.store._write('UPDATE sticker_assets SET tags=?', ('[{"injected":"开心"}]',))
        self.assertEqual(self.candidates(), [])


if __name__ == '__main__':
    unittest.main()
