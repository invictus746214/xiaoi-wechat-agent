import json
import unittest

from pipeline_health import PipelineHealth
from store import Store


class PipelineDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.health = PipelineHealth(self.store)
        self.job = dict(event_id='private-event', account='private-account', username='private-person',
                        kind='private', message='秘密正文', voice_transcript='秘密转写')

    def tearDown(self):
        self.store.close()

    def test_voice_phase_report_is_scene_scoped_persisted_and_deduplicated(self):
        self.assertTrue(self.health.note_phase(self.job, 'native_read', 0.25, now=100))
        self.assertTrue(self.health.note_phase(self.job, 'recognition', 1.5, now=101))
        self.health.note_phase(self.job, 'recognition', 99, now=102)
        self.health.note_phase(dict(self.job, event_id='second'), 'recognition', 2.5, now=103)
        self.health.note_phase(dict(self.job, username='other'), 'recognition', 100, now=103)
        self.health.note_phase(dict(self.job, account='other-account'), 'recognition', 200, now=103)
        self.health = PipelineHealth(self.store)
        report = self.health.report(self.job, now=104)
        self.assertIn('读取原语音：1次，平均0.25秒，最长0.25秒', report)
        self.assertIn('语音识别：2次，平均2.00秒，最长2.50秒', report)
        for secret in ('private-event', 'private-account', 'private-person', '秘密正文', '秘密转写'):
            self.assertNotIn(secret, report)
            self.assertNotIn(secret, json.dumps(self.store._rows('SELECT * FROM pipeline_phase'), ensure_ascii=False))
        self.assertNotIn('语音识别', self.health.report(self.job, now=86505))

    def test_invalid_phase_metadata_is_rejected_without_leaking_input(self):
        self.assertTrue(hasattr(self.health, 'note_phase'), 'safe phase recorder is missing')
        for name, duration in (('秘密正文', 1), ('recognition', '秘密转写'), ('total', -1),
                               ('worker', float('nan')), ('decode', float('inf')), ('model', True),
                               ('total', 86401), ('total', None)):
            self.assertFalse(self.health.note_phase(self.job, name, duration, now=100))
        self.assertFalse(self.health.note_phase(dict(self.job, event_id=''), 'total', 1, now=100))
        self.assertFalse(self.health.note_phase(self.job, 'total', 1, now=float('nan')))
        self.assertEqual(self.store._rows('SELECT * FROM pipeline_phase'), [])
        self.store.close()
        self.assertFalse(self.health.note_phase(self.job, 'total', 1))
        self.store = Store()

    def test_runtime_status_is_latest_account_scoped_and_becomes_stale(self):
        self.assertTrue(hasattr(self.health, 'runtime'), 'runtime status recorder is missing')
        self.assertIn('当前是否在线未知', self.health.report(self.job, now=100))
        self.assertTrue(self.health.runtime('private-account', 'online', now=100))
        self.assertIn('最近检查在线', self.health.report(self.job, now=101))
        self.assertTrue(self.health.runtime('private-account', 'reader_error', now=102))
        self.health.runtime('private-account', 'online', now=99)
        self.health.runtime('other-account', 'offline', now=103)
        self.health = PipelineHealth(self.store)
        report = self.health.report(self.job, now=104)
        self.assertIn('消息读取异常', report)
        self.assertIn('2秒前', report)
        self.assertNotIn('最近检查在线', report)
        stale = self.health.report(self.job, now=403)
        self.assertIn('状态已过期', stale)
        self.assertIn('当前是否在线未知', stale)
        self.assertNotIn('private-account', json.dumps(self.store._rows('SELECT * FROM pipeline_runtime')))
        self.assertFalse(self.health.runtime('private-account', '秘密错误正文', now=105))
        self.assertFalse(self.health.runtime('', 'online', now=105))
        self.assertFalse(self.health.runtime('private-account', 'online', now=float('inf')))
        self.assertNotIn('秘密错误正文', json.dumps(self.store._rows('SELECT * FROM pipeline_runtime'), ensure_ascii=False))
        self.store.close()
        self.assertFalse(self.health.runtime('private-account', 'online'))
        self.store = Store()

    def test_new_nonreply_reasons_are_friendly_and_current_scene_only(self):
        expected = [('obsolete', '旧回复已撤回'), ('requeued', '新发言已重新排队'),
                    ('voice_timeout', '语音识别超时'), ('voice_unavailable', '语音识别暂不可用'),
                    ('reader_error', '消息读取异常'), ('offline', '微信离线'),
                    ('account_mismatch', '微信账号不匹配')]
        for number, (reason, label) in enumerate(expected):
            with self.subTest(reason=reason):
                event = dict(self.job, event_id='event'+str(number))
                self.assertTrue(self.health.note(event, 'skipped', reason, now=100))
                self.assertIn(label+'：1', self.health.report(self.job, now=101))
        self.assertNotIn('旧回复已撤回', self.health.report(dict(self.job, username='other'), now=101))

    def test_phase_table_is_hard_bounded_and_purge_removes_old_timings(self):
        self.assertTrue(hasattr(self.health, 'note_phase'), 'bounded phase recorder is missing')
        for number in range(4100):
            self.health.note_phase(dict(self.job, event_id='event'+str(number)), 'total', 0.5, now=100+number)
        bounds = self.store._rows('SELECT COUNT(*) AS n,MIN(updated) AS oldest,MAX(updated) AS latest FROM pipeline_phase')[0]
        self.assertEqual(bounds, dict(n=4096, oldest=104, latest=4199))
        self.health.purge(now=14*86400+4300)
        self.assertEqual(self.store._rows('SELECT * FROM pipeline_phase'), [])


if __name__ == '__main__':
    unittest.main()
