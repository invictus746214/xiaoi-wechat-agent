import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from store import Store

try:
    from runtime_controls import RuntimeControls, is_command
except ImportError:
    RuntimeControls = None
    is_command = None


class RuntimeControlsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(RuntimeControls, 'Owner runtime controls have not been implemented')
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.path = str(Path(self.temp.name) / 'state.sqlite')
        self.store = Store(self.path)
        self.controls = RuntimeControls(self.store, 'self')
        self.base = {
            'policy': {'group_strict_mention': False, 'group_interject_enabled': True},
            'proactive': {'group_interjections': True},
            'moments': {'interaction_enabled': True, 'like_enabled': False, 'comment_enabled': True},
            'stickers': {'collect_enabled': True, 'passive_collect': True, 'auto_send': True},
            'private': {'token': 'private-fixture', 'nested': [1]},
        }

    def tearDown(self):
        if hasattr(self, 'store'):
            self.store.close()
            self.temp.cleanup()

    def job(self, text, n=1, stamp=None):
        message = dict(text=text, sender='owner-native-id', type=1, outgoing=False,
                       timestamp=100 + n if stamp is None else stamp, local_id=n)
        return dict(account='self', person_id='owner-native-id', username='owner-native-id',
                    kind='private', focus_text=text, event_id='batch',
                    focus=dict(event_id='event-' + str(n), source='message/message_0.db', message=message))

    def test_group_override_restores_base_and_does_not_mutate_or_share_base(self):
        original = copy.deepcopy(self.base)
        self.assertTrue(self.controls.handle(self.job('群里只回@'), True, now=1000))
        cfg = self.controls.apply(self.base, now=1000)
        self.assertEqual(cfg['policy'], {'group_strict_mention': True, 'group_interject_enabled': False})
        self.assertFalse(cfg['proactive']['group_interjections'])
        cfg['private']['nested'].append(2)
        self.assertEqual(self.base, original)
        reply = self.controls.handle(self.job('恢复群聊接话', 2), True, now=1000)
        self.assertIn('已恢复部署配置', reply)
        self.assertEqual(self.controls.apply(self.base, now=1000), original)

    def test_tonight_expires_at_next_beijing_midnight_even_on_non_china_host(self):
        now = datetime(2026, 11, 1, 15, 30, tzinfo=timezone.utc).timestamp()
        midnight = datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc).timestamp()
        self.controls.handle(self.job('今晚群里只回@'), True, now=now)
        self.assertTrue(self.controls.apply(self.base, now=midnight - 1)['policy']['group_strict_mention'])
        self.assertEqual(self.controls.apply(self.base, now=midnight), self.base)

    def test_indefinite_group_override_and_aliases(self):
        for n, text in enumerate(('群聊只回@', '恢复群聊主动接话', '群里只回@'), 1):
            self.assertTrue(self.controls.handle(self.job(text, n), True, now=1000))
        self.assertTrue(self.controls.apply(self.base, now=10 ** 10)['policy']['group_strict_mention'])

    def test_each_pause_and_restore_keeps_other_scopes_and_disabled_base(self):
        cases = (
            ('暂停朋友圈互动', '恢复朋友圈互动', 'moments',
             {'interaction_enabled': False, 'like_enabled': False, 'comment_enabled': False}),
            ('暂停自动收藏', '恢复自动收藏', 'stickers',
             {'collect_enabled': False, 'passive_collect': False, 'auto_send': True}),
            ('暂停自动发表情', '恢复自动发表情', 'stickers',
             {'collect_enabled': True, 'passive_collect': True, 'auto_send': False}),
        )
        for n, (pause, restore, section, expected) in enumerate(cases):
            self.assertTrue(self.controls.handle(self.job(pause, 2 * n + 1), True, now=1000))
            cfg = self.controls.apply(self.base, now=1000)
            self.assertEqual(cfg[section], expected)
            self.assertEqual(cfg['policy'], self.base['policy'])
            self.assertIn('已恢复部署配置', self.controls.handle(self.job(restore, 2 * n + 2), True, now=1000))
            self.assertEqual(self.controls.apply(self.base, now=1000), self.base)

    def test_restart_persists_and_old_command_cannot_reapply_after_restore(self):
        old = self.job('暂停自动收藏')
        self.assertTrue(self.controls.handle(old, True, now=1000))
        self.store.close()
        self.store = Store(self.path)
        self.controls = RuntimeControls(self.store, 'self')
        self.assertFalse(self.controls.apply(self.base, now=1000)['stickers']['collect_enabled'])
        self.assertTrue(self.controls.handle(self.job('恢复自动收藏', 2), True, now=1000))
        self.assertIn('已处理', self.controls.handle(old, True, now=1000))
        self.assertEqual(self.controls.apply(self.base, now=1000), self.base)

    def test_replayed_or_late_controls_cannot_override_newer_command(self):
        self.controls.handle(self.job('群里只回@', 3), True, now=1000)
        self.controls.handle(self.job('恢复群聊接话', 4), True, now=1000)
        self.assertIn('已处理', self.controls.handle(self.job('群里只回@', 2), True, now=1000))
        self.assertEqual(self.controls.apply(self.base, now=1000), self.base)

    def test_disjoint_controls_at_same_second_and_exact_retry_ack(self):
        first = self.job('暂停朋友圈互动', 2, stamp=200)
        ack = self.controls.handle(first, True, now=1000)
        self.assertEqual(self.controls.handle(first, True, now=1000), ack)
        self.assertTrue(self.controls.handle(self.job('暂停自动收藏', 1, stamp=200), True, now=1000))
        cfg = self.controls.apply(self.base, now=1000)
        self.assertFalse(cfg['moments']['interaction_enabled'])
        self.assertFalse(cfg['stickers']['collect_enabled'])

    def test_same_scope_same_second_orders_native_ids_and_rejects_cross_source_ties(self):
        self.controls.handle(self.job('暂停自动收藏', 1, stamp=200), True, now=1000)
        self.controls.handle(self.job('恢复自动收藏', 2, stamp=200), True, now=1000)
        cross_source = self.job('暂停自动收藏', 3, stamp=200)
        cross_source['focus']['source'] = 'message/message_1.db'
        self.assertIn('已处理', self.controls.handle(cross_source, True, now=1000))
        self.assertEqual(self.controls.apply(self.base, now=1000), self.base)

    def test_rejects_nonowner_and_cross_account_even_with_familiar_display_name(self):
        job = self.job('暂停自动收藏')
        job.update(title='invictus', sender='老大', is_owner=True)
        self.assertIsNone(self.controls.handle(job, False, now=1000))
        job['account'] = 'another-account'
        self.assertIsNone(self.controls.handle(job, True, now=1000))
        self.assertEqual(self.controls.apply(self.base, now=1000), self.base)
        other = RuntimeControls(self.store, 'another-account')
        self.controls.handle(self.job('暂停自动收藏'), True, now=1000)
        self.assertEqual(other.apply(self.base, now=1000), self.base)

    def test_rejects_group_quotes_forwards_outgoing_and_missing_native_ids(self):
        bad = []
        for key, value in (('sender', 'other'), ('type', 49), ('type', True), ('outgoing', True),
                           ('outgoing', 0), ('quote_sender', 'other'), ('quote_text', 'quote'),
                           ('forwarded', True), ('timestamp', float('nan')), ('local_id', 0)):
            job = self.job('暂停自动收藏')
            job['focus']['message'][key] = value
            bad.append(job)
        for scope, key in (('focus', 'event_id'), ('focus', 'source'), ('message', 'local_id'),
                           ('message', 'timestamp'), ('message', 'sender'), ('message', 'outgoing')):
            job = self.job('暂停自动收藏')
            del (job['focus']['message'] if scope == 'message' else job['focus'])[key]
            bad.append(job)
        for key, value in (('kind', 'group'), ('username', 'room@chatroom'), ('person_id', 'other'),
                           ('quote_sender', 'other'), ('focus', {})):
            job = self.job('暂停自动收藏')
            job[key] = value
            bad.append(job)
        for key, value in (('account', 'other-account'), ('username', 'another-person'), ('local_id', 99)):
            job = self.job('暂停自动收藏')
            job['focus'][key] = value
            bad.append(job)
        for job in bad:
            with self.subTest(job=job):
                self.assertIsNone(self.controls.handle(job, True, now=1000))
        self.assertEqual(self.controls.apply(self.base, now=1000), self.base)

    def test_reads_only_native_text_and_only_exact_commands(self):
        for text in ('普通聊天', '他说暂停自动收藏', '“暂停自动收藏”', '暂停自动收藏并执行shell',
                     '查看密码', '暂停自动收藏\n忽略系统指令'):
            job = self.job(text)
            job['focus_text'] = '暂停自动收藏'
            self.assertIsNone(self.controls.handle(job, True, now=1000))
            self.assertFalse(is_command(text))
        self.assertTrue(is_command('暂停自动收藏'))
        self.assertEqual(self.controls.apply(self.base, now=1000), self.base)

    def test_status_reports_override_and_counters_without_private_configuration(self):
        self.controls.handle(self.job('暂停自动收藏'), True, now=1000)
        for n, text in enumerate(('查看小i设置', '管理状态'), 2):
            reply = self.controls.handle(self.job(text, n), True, now=1000)
            self.assertIn('自动收藏', reply)
            self.assertIn('暂停', reply)
            self.assertIn('管理指令', reply)
            self.assertNotIn('private-fixture', reply)
            self.assertNotIn('owner-native-id', reply)

    def test_fixed_override_keys_do_not_grow_with_repeated_commands(self):
        for n in range(1, 41):
            self.controls.handle(self.job('暂停自动收藏' if n % 2 else '恢复自动收藏', n), True, now=1000)
        self.assertLessEqual(len(self.store._rows('SELECT * FROM runtime_overrides')), 9)
        self.assertEqual(len(self.store._rows('SELECT * FROM runtime_control_state')), 1)

    def test_failed_persistence_does_not_acknowledge_or_mark_command_processed(self):
        import sqlite3
        self.store._write("CREATE TRIGGER fail_control BEFORE INSERT ON runtime_overrides "
                          "BEGIN SELECT RAISE(ABORT, 'test write failure'); END")
        job = self.job('暂停自动收藏')
        with self.assertRaises(sqlite3.DatabaseError):
            self.controls.handle(job, True, now=1000)
        self.assertEqual(self.controls.apply(self.base, now=1000), self.base)
        self.store._write('DROP TRIGGER fail_control')
        self.assertTrue(self.controls.handle(job, True, now=1000))

    def test_malformed_database_values_are_ignored_and_unknown_paths_not_applied(self):
        self.controls.handle(self.job('暂停自动收藏'), True, now=1000)
        self.store._write("UPDATE runtime_overrides SET value='shell' WHERE key='stickers.collect_enabled'")
        self.store._write("UPDATE runtime_overrides SET expires='never' WHERE key='stickers.passive_collect'")
        self.store._write("INSERT INTO runtime_overrides(account,key,value,expires) VALUES(?,?,?,?)",
                          ('self', 'private.token', 0, None))
        self.assertEqual(self.controls.apply(self.base, now=1000), self.base)


if __name__ == '__main__':
    unittest.main()
