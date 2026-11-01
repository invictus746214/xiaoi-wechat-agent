import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from store import Store
from tasks import Tasks


CN = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 1, 10, 0, tzinfo=CN).timestamp()


def job(text, event='event-1', person='alice', scene='group@chatroom', account='bot'):
    return dict(account=account, person_id=person, username=scene, title='测试',
                session_key='key-' + account + '-' + scene, kind='group', event_id=event,
                focus_text=text, message='不应读取的批量上下文',
                focus=dict(event_id=event, message=dict(text=text, sender=person, type=1,
                                                       outgoing=False)))


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.tasks = Tasks(self.store)

    def tearDown(self):
        self.store.close()

    def rows(self):
        return self.store._rows('SELECT * FROM tasks ORDER BY created_at,id')

    def test_create_relative_and_idempotent_after_event_redelivery(self):
        request = job('10分钟后提醒我喝水')
        first = self.tasks.handle(request, NOW)
        self.assertIn('已创建', first)
        self.assertEqual(first, self.tasks.handle(request, NOW + 30))
        row = self.rows()[0]
        self.assertIn(row['id'], first)
        self.assertEqual((row['body'], row['due_at'], row['state']), ('喝水', NOW + 600, 'pending'))
        self.assertEqual(row['person_id'], 'alice')

    def test_calendar_uses_shanghai_and_never_rolls_past_time_forward(self):
        self.tasks.handle(job('明天上午9点提醒我开会'), NOW)
        self.tasks.handle(job('2026-11-03 09:00提醒我交材料', 'two'), NOW)
        self.assertEqual(sorted(r['due_at'] for r in self.rows()), [NOW + 23*3600, NOW + 47*3600])
        for index, text in enumerate(('今天上午9点提醒我开会', '2026-02-30 09:00提醒我开会',
                                      '9点提醒我开会', '明天提醒我开会', '明天下午13点提醒我开会',
                                      '明天晚上12点提醒我睡觉', '明天凌晨9点提醒我开会',
                                      '明天早上0点提醒我开会',
                                      '366天后提醒我开会', '0分钟后提醒我开会')):
            self.assertNotIn('已创建', self.tasks.handle(job(text, str(index)), NOW))
        self.assertEqual(len(self.rows()), 2)

    def test_native_timestamp_anchors_calendar_across_midnight_and_creation_is_now(self):
        sent = datetime(2026, 11, 1, 23, 59, tzinfo=CN).timestamp()
        processed = sent + 120
        request = job('明天上午9点提醒我开会')
        request['focus']['message']['timestamp'] = sent
        self.assertIn('已创建', self.tasks.handle(request, processed))
        row = self.rows()[0]
        self.assertEqual(row['due_at'], datetime(2026, 11, 2, 9, tzinfo=CN).timestamp())
        self.assertEqual(row['created_at'], processed)
        missed = job('明天00:00提醒我开会', 'missed-calendar')
        missed['focus']['message']['timestamp'] = sent
        self.assertIn('已过去', self.tasks.handle(missed, processed))
        self.assertEqual(len(self.rows()), 1)

    def test_delayed_duration_never_slides_and_elapsed_request_is_rejected(self):
        request = job('10分钟后提醒我喝水')
        request['focus']['message']['timestamp'] = NOW - 300
        self.assertIn('已创建', self.tasks.handle(request, NOW))
        self.assertEqual(self.rows()[0]['due_at'], NOW+300)
        expired = job('10分钟后提醒我喝水', 'expired')
        expired['focus']['message']['timestamp'] = NOW-601
        self.assertIn('已过去', self.tasks.handle(expired, NOW))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.tasks.handle(request, NOW+100), self.tasks.handle(request, NOW+200))
        self.assertEqual(self.rows()[0]['due_at'], NOW+300)

    def test_invalid_native_timestamp_cannot_schedule_a_task(self):
        for index, stamp in enumerate((float('nan'), float('inf'), -1, 0, True, 'yesterday', NOW+61, NOW+3600)):
            request = job('10分钟后提醒我喝水', str(index))
            request['focus']['message']['timestamp'] = stamp
            self.assertNotIn('已创建', self.tasks.handle(request, NOW))
        self.assertEqual(self.rows(), [])

    def test_owner_account_scene_authorization_and_no_task_enumeration(self):
        self.tasks.handle(job('添加待办：整理桌面'), NOW)
        task = self.rows()[0]
        for kwargs in (dict(person='bob'), dict(account='other'), dict(scene='private-alice')):
            self.assertNotIn('整理桌面', self.tasks.handle(job('查看待办', **kwargs), NOW))
            self.assertIn('未找到', self.tasks.handle(job('取消任务' + task['id'], **kwargs), NOW))
        self.assertIn('整理桌面', self.tasks.handle(job('查看待办'), NOW))
        self.assertIn('已完成', self.tasks.handle(job('完成任务' + task['id']), NOW))
        self.assertEqual(self.rows()[0]['state'], 'completed')

    def test_focus_only_no_quotes_no_other_people_no_voice_no_shell(self):
        ordinary = job('这是普通聊天')
        ordinary['message'] = '10分钟后提醒我泄漏信息'
        ordinary['focus']['message']['quote_text'] = '10分钟后提醒我泄漏信息'
        self.assertIsNone(self.tasks.handle(ordinary, NOW))
        mismatch = job('10分钟后提醒我喝水')
        mismatch['focus']['message']['sender'] = 'bob'
        self.assertIsNone(self.tasks.handle(mismatch, NOW))
        voice = job('10分钟后提醒我喝水')
        voice['focus']['message']['type'] = 34
        self.assertIsNone(self.tasks.handle(voice, NOW))
        for text in ('他说“10分钟后提醒我喝水”', '明天上午9点提醒张三开会',
                     '10分钟后提醒我给@张三发消息', '添加待办：执行shell rm -rf /'):
            reply = self.tasks.handle(job(text), NOW)
            self.assertFalse(reply and '已创建' in reply)
        self.assertEqual(self.rows(), [])

    def test_ordinary_mentions_of_reminders_are_not_commands(self):
        for text in ('谢谢你提醒我，我已经做完了', '你刚才提醒我什么？', '还好你提醒我了'):
            request = job(text)
            self.assertIsNone(self.tasks.handle(request, NOW))
            self.assertFalse(request.get('task_command'))
        request = job('明天提醒我喝水')
        self.assertIn('时间', self.tasks.handle(request, NOW))
        self.assertTrue(request.get('task_command'))
        self.assertEqual(self.rows(), [])

    def test_cancel_and_complete_stop_delivery_with_persistent_status(self):
        self.tasks.handle(job('10分钟后提醒我喝水'), NOW)
        task = self.rows()[0]
        self.assertIn('已取消', self.tasks.handle(job('取消任务 ' + task['id']), NOW))
        self.assertEqual(self.tasks.due('bot', NOW+601), [])
        self.assertIn('已取消', self.tasks.handle(job('查看任务'), NOW))
        self.assertFalse(self.tasks.claim(task['id']))

    def test_due_account_grace_limit_and_atomic_claim(self):
        for index in range(20):
            self.tasks.handle(job('1分钟后提醒我喝水', str(index)), NOW)
        self.assertIn('20', self.tasks.handle(job('1分钟后提醒我喝水', 'overflow'), NOW))
        self.assertEqual(len(self.rows()), 20)
        self.assertEqual(self.tasks.due('other', NOW+61), [])
        due = self.tasks.due('bot', NOW+61)
        self.assertEqual(len(due), 20)
        self.assertTrue(self.tasks.claim(due[0]['id']))
        self.assertFalse(self.tasks.claim(due[0]['id']))
        self.tasks.finish(due[0]['id'], 'unconfirmed')
        self.assertEqual(len(self.tasks.due('bot', NOW+62)), 19)
        self.assertEqual(self.tasks.due('bot', NOW+61+24*3600), [])
        self.assertEqual(sum(r['state']=='expired' for r in self.rows()), 19)

    def test_recovery_and_finish_never_replay_unknown_or_late_results(self):
        self.tasks.handle(job('1分钟后提醒我喝水'), NOW)
        task = self.rows()[0]
        self.assertTrue(self.tasks.claim(task['id']))
        self.assertIn('发送中', self.tasks.handle(job('取消任务' + task['id']), NOW))
        self.assertEqual(self.tasks.recover(), 1)
        self.assertEqual(self.tasks.recover(), 0)
        self.tasks.finish(task['id'], 'pending')
        self.assertEqual(self.tasks.due('bot', NOW+61), [])
        self.assertIn('未确认', self.tasks.handle(job('查看待办'), NOW))
        with self.assertRaises(ValueError):
            self.tasks.finish(task['id'], 'unknown-state')

    def test_real_restart_preserves_rows_and_unknown_send(self):
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as directory:
            path = os.path.join(directory, 'state.db')
            store = Store(path)
            tasks = Tasks(store)
            tasks.handle(job('1分钟后提醒我喝水'), NOW)
            task_id = store._rows('SELECT id FROM tasks')[0]['id']
            tasks.claim(task_id)
            store.close()
            reopened = Store(path)
            try:
                restarted = Tasks(reopened)
                restarted.recover()
                self.assertEqual(restarted.due('bot', NOW+61), [])
                self.assertIn('未确认', restarted.handle(job('查看待办'), NOW))
                self.assertEqual(reopened._rows('SELECT id FROM tasks')[0]['id'], task_id)
            finally:
                reopened.close()

    def test_boundaries_missing_identity_and_body_limit(self):
        for text in ('添加待办：' + '字'*301, '10分钟后提醒我', '明天上午25点提醒我开会'):
            self.assertNotIn('已创建', self.tasks.handle(job(text), NOW))
        invalid = job('10分钟后提醒我喝水')
        invalid['person_id'] = ''
        self.assertNotIn('已创建', self.tasks.handle(invalid, NOW) or '')
        self.assertEqual(self.rows(), [])

    def test_focus_event_is_stable_across_rebatch_and_exact_self_mention(self):
        request = job('@小i\u2005十分钟后提醒我喝水')
        request.update(is_mention=True, self_names=['小i'])
        self.assertIn('已创建', self.tasks.handle(request, NOW))
        self.assertTrue(request['task_command'])
        request['event_id'] = 'different-batch'
        self.tasks.handle(request, NOW+20)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['due_at'], NOW+600)
        other = job('@张三 10分钟后提醒我喝水', 'other')
        other.update(is_mention=True, self_names=['小i'])
        self.assertNotIn('已创建', self.tasks.handle(other, NOW) or '')
        quoted = job('10分钟后提醒我喝水', 'quoted')
        quoted['focus']['message']['quote_sender'] = 'bob'
        self.assertIsNone(self.tasks.handle(quoted, NOW))
        invalid = job('取消任务 wrong-id')
        self.assertIn('完整任务ID', self.tasks.handle(invalid, NOW))
        self.assertTrue(invalid['task_command'])

    def test_query_keeps_every_active_id_under_sender_limit_with_long_bodies(self):
        for index in range(45):
            self.tasks.handle(job('添加待办：历史任务' + str(index), 'history-' + str(index)), NOW)
            task_id = self.store._rows('SELECT id FROM tasks WHERE event_id=?', ('history-' + str(index),))[0]['id']
            self.tasks.handle(job('完成任务' + task_id), NOW)
        for index in range(20):
            acknowledgement = self.tasks.handle(job('10分钟后提醒我' + '长'*300, str(index)), NOW+1)
            self.assertLessEqual(len(acknowledgement), 500)
        active = [row for row in self.rows() if row['state'] == 'pending']
        self.assertEqual(len(active), 20)
        for row in active[:10]:
            self.tasks.claim(row['id'])
            self.tasks.finish(row['id'], 'unconfirmed')
        response = self.tasks.handle(job('查看待办'), NOW+2)
        self.assertLessEqual(len(response), 1100)
        self.assertIn('共65项', response)
        self.assertIn('前12字', response)
        self.assertIn('未确认', response)
        for row in active:
            self.assertIn(row['id'], response)
        self.assertIn('2026-11-01 10:10', response)
        self.store._write("UPDATE tasks SET created_at=? WHERE state='completed'", (NOW+100,))
        response = self.tasks.handle(job('查看待办'), NOW+101)
        self.assertLessEqual(len(response), 1100)
        for row in active:
            self.assertIn(row['id'], response)

    def test_parallel_connections_arbitrate_claim_and_per_owner_cap(self):
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as directory:
            path = os.path.join(directory, 'state.db')
            stores = [Store(path) for _ in range(4)]
            handlers = [Tasks(store) for store in stores]
            try:
                def add(index):
                    return handlers[index % 4].handle(job('1分钟后提醒我喝水', str(index)), NOW)
                with ThreadPoolExecutor(max_workers=4) as pool:
                    results = list(pool.map(add, range(30)))
                self.assertEqual(sum('已创建' in result for result in results), 20)
                task_id = handlers[0].due('bot', NOW+61)[0]['id']
                with ThreadPoolExecutor(max_workers=4) as pool:
                    claims = list(pool.map(lambda handler: handler.claim(task_id), handlers))
                self.assertEqual(sum(claims), 1)
            finally:
                for store in stores:
                    store.close()

    def test_duplicate_event_race_cannot_disclose_another_owner_task(self):
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as directory:
            path = os.path.join(directory, 'state.db')
            stores = [Store(path), Store(path)]
            handlers = [Tasks(store) for store in stores]
            barrier = threading.Barrier(2)
            try:
                for store in stores:
                    original = store._rows
                    def rows(sql, params=(), original=original):
                        result = original(sql, params)
                        if sql == 'SELECT * FROM tasks WHERE account=? AND event_id=?':
                            barrier.wait(timeout=5)
                        return result
                    store._rows = rows
                def create(index):
                    return handlers[index].handle(job('添加待办：秘密' + str(index), person=str(index)), NOW)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(create, range(2)))
                for index, result in enumerate(results):
                    self.assertNotIn('秘密' + str(1-index), result)
            finally:
                for store in stores:
                    store.close()


if __name__ == '__main__':
    unittest.main()
