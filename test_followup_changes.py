import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from followups import Followups
from store import Store
from tasks import Tasks
from test_followups import NOW, job


class FollowupChangeTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.followups = Followups(self.store)
        self.tasks = Tasks(self.store)

    def tearDown(self):
        self.store.close()

    def rows(self):
        return self.store._rows('SELECT * FROM tasks ORDER BY created_at,rowid')

    def arrange(self, text='明天下午3点面试结束后问问我进展', event='original'):
        self.followups.handle(job(text, event), NOW)
        return self.rows()[-1]

    def test_reschedule_retracts_old_time_and_requires_explicit_consent(self):
        old = self.arrange()
        response = self.followups.handle(job('我面试改期到后天下午3点', 'change'), NOW+5)
        self.assertIn('同意', response or '')
        self.assertEqual(self.rows()[0]['state'], 'cancelled')
        self.assertEqual(self.tasks.due('bot', NOW+31*3600), [])
        self.assertEqual(len(self.rows()), 1)
        self.assertIsNone(self.followups.handle(job('好的', 'chitchat'), NOW+6))
        receipt = self.followups.handle(job('好，之后问问我', 'confirm'), NOW+10)
        self.assertIn('2026-11-03 17:00', receipt)
        self.assertEqual([r['state'] for r in self.rows()], ['cancelled', 'pending'])
        self.assertEqual(self.rows()[1]['body'], old['body'])
        self.assertEqual(self.followups.handle(job('好，之后问问我', 'confirm'), NOW+20), receipt)
        self.assertEqual(len(self.rows()), 2)

    def test_vague_reschedule_stops_old_question_until_clear_future_time_and_consent(self):
        self.arrange()
        response = self.followups.handle(job('我面试推迟了', 'change'), NOW+5)
        self.assertIn('具体', response or '')
        self.assertEqual(self.rows()[0]['state'], 'cancelled')
        for n, text in enumerate(('同意调整跟进', '好，周一下午3点问问我', '好，明天问问我',
                                  '好，今天上午9点问问我')):
            self.assertNotIn('已创建', self.followups.handle(job(text, 'bad'+str(n)), NOW+10) or '')
            self.assertEqual(len(self.rows()), 1)
        receipt = self.followups.handle(job('可以，后天上午9点问问我', 'clear'), NOW+15)
        self.assertIn('2026-11-03 09:00', receipt)
        self.assertEqual(self.rows()[1]['state'], 'pending')

    def test_cancellation_completion_and_result_stop_only_matching_event(self):
        for n, text in enumerate(('面试取消了', '不用再问我面试', '不要再问我面试',
                                  '面试结束了', '已经面试完了', '我面试通过了', '面试没通过')):
            with self.subTest(text=text):
                self.store.close()
                self.store = Store()
                self.followups = Followups(self.store)
                self.tasks = Tasks(self.store)
                old = self.arrange()
                exam = self.arrange('后天上午9点考试结束后问问我', 'exam')
                self.followups.handle(job(text, 'change'+str(n)), NOW+5)
                self.assertEqual(self.rows()[0]['state'], 'cancelled')
                self.assertEqual(self.rows()[1]['state'], 'pending')
                self.assertIn(exam['id'], self.followups.handle(job('查看跟进', 'list'), NOW+6))
                self.assertNotIn(old['id'], [r['id'] for r in self.tasks.due('bot', NOW+31*3600)])

    def test_multiple_matching_tasks_are_held_until_explicit_target(self):
        first = self.arrange()
        second = self.arrange('后天上午9点面试结束后问问我', 'second')
        response = self.followups.handle(job('我面试推迟了', 'change'), NOW+5)
        self.assertIn(first['id'], response or '')
        self.assertIn(second['id'], response)
        self.assertEqual([r['state'] for r in self.rows()], ['pending', 'pending'])
        self.assertFalse(self.followups.can_deliver(first, NOW+31*3600))
        self.assertFalse(self.followups.can_deliver(second, NOW+47*3600))
        self.followups.handle(job('好，之后问问我', 'ambiguous'), NOW+6)
        self.assertEqual(len(self.rows()), 2)
        response = self.followups.handle(job('调整跟进 '+first['id']+' 到后天下午3点', 'target'), NOW+10)
        self.assertIn('同意', response)
        self.assertEqual([r['state'] for r in self.rows()], ['cancelled', 'pending'])
        self.assertTrue(self.followups.can_deliver(second, NOW+47*3600))
        self.followups.handle(job('同意调整跟进', 'confirm'), NOW+15)
        self.assertEqual([r['state'] for r in self.rows()], ['cancelled', 'pending', 'pending'])

    def test_natural_change_and_confirmation_are_private_native_and_source_scoped(self):
        self.arrange()
        requests = [job('面试取消了', 'bob', person='bob'),
                    job('面试取消了', 'other', account='other')]
        wrong_scene = job('面试取消了', 'scene')
        wrong_scene['session_key'] = 'other-scene'
        requests.append(wrong_scene)
        for mutate in (lambda j: j.update(kind='group'),
                       lambda j: j['focus']['message'].update(quote_sender='bob'),
                       lambda j: j['focus'].update(event_id='forged'),
                       lambda j: j['focus']['message'].update(outgoing=True)):
            invalid = job('面试取消了', 'invalid')
            mutate(invalid)
            requests.append(invalid)
        for request in requests:
            self.followups.handle(request, NOW+5)
            self.assertEqual(self.rows()[0]['state'], 'pending')
        self.followups.handle(job('我面试改期到后天下午3点', 'change'), NOW+10)
        self.followups.handle(job('好，之后问问我', 'bob-confirm', person='bob'), NOW+15)
        self.assertEqual(len(self.rows()), 1)

    def test_sending_and_unknown_are_never_retried_deleted_or_cancelled(self):
        for state in ('sending', 'unconfirmed'):
            old = self.arrange(event=state)
            self.tasks.claim(old['id'])
            if state == 'unconfirmed':
                self.tasks.finish(old['id'], state)
            reply = self.followups.handle(job('我面试改期到后天下午3点', 'change'+state), NOW+5)
            self.assertIn('未确认', reply or '')
            self.followups.handle(job('同意调整跟进', 'confirm'+state), NOW+10)
            self.assertEqual(self.rows()[-1]['state'], state)
        self.assertEqual(len(self.rows()), 2)

    def test_cancellation_invalidates_open_proposal_and_withdrawn_event_cannot_revive_it(self):
        original = job('明天下午3点面试', 'offer')
        self.followups.observe(original, NOW)
        self.followups.handle(job('面试取消了', 'cancel'), NOW+5)
        self.assertIsNone(self.followups.observe(original, NOW+10))
        self.followups.handle(job('好，之后问问我', 'confirm'), NOW+15)
        self.assertEqual(self.rows(), [])

    def test_cancellation_after_reschedule_invalidates_pending_consent(self):
        self.arrange()
        self.followups.handle(job('我面试改期到后天下午3点', 'change'), NOW+5)
        self.followups.handle(job('面试取消了', 'cancel'), NOW+10)
        self.followups.handle(job('好，之后问问我', 'confirm'), NOW+15)
        self.assertEqual([r['state'] for r in self.rows()], ['cancelled'])

    def test_change_proposal_expires_without_reactivating_original(self):
        self.arrange()
        self.followups.handle(job('我面试改期到后天下午3点', 'change'), NOW+5)
        confirm = job('同意调整跟进', 'late')
        confirm['focus']['message']['timestamp'] = NOW+601
        self.followups.handle(confirm, NOW+601)
        self.assertEqual([r['state'] for r in self.rows()], ['cancelled'])

    def test_restart_replays_exact_receipts_and_does_not_duplicate_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'state.db')
            store = Store(path)
            followups = Followups(store)
            followups.handle(job('明天下午3点面试结束后问问我', 'original'), NOW)
            request = job('我面试改期到后天下午3点', 'change')
            response = followups.handle(request, NOW+5)
            store.close()
            store = Store(path)
            try:
                followups = Followups(store)
                self.assertEqual(followups.handle(request, NOW+10), response)
                confirm = job('同意调整跟进', 'confirm')
                receipt = followups.handle(confirm, NOW+15)
                self.assertIn('已创建', receipt or '')
                followups = Followups(store)
                self.assertEqual(followups.handle(confirm, NOW+20), receipt)
                self.assertEqual(len(store._rows('SELECT * FROM tasks')), 2)
            finally:
                store.close()

    def test_cancel_by_id_resolves_ambiguity_without_cancelling_other_task(self):
        first = self.arrange()
        second = self.arrange('后天上午9点面试结束后问问我', 'second')
        self.followups.handle(job('面试取消了', 'ambiguous'), NOW+5)
        self.followups.handle(job('取消跟进 '+first['id'], 'select'), NOW+10)
        self.assertEqual([r['state'] for r in self.rows()], ['cancelled', 'pending'])
        self.assertTrue(self.followups.can_deliver(second, NOW+47*3600))

    def test_cancel_original_by_id_revokes_replacement_consent(self):
        original = self.arrange()
        self.followups.handle(job('面试改期到后天下午3点', 'change'), NOW+5)
        self.followups.handle(job('取消跟进 '+original['id'], 'cancel'), NOW+10)
        self.followups.handle(job('同意调整跟进', 'confirm'), NOW+15)
        self.assertEqual([r['state'] for r in self.rows()], ['cancelled'])

    def test_fresh_clear_fact_after_vague_change_still_requires_consent(self):
        self.arrange()
        self.followups.handle(job('面试推迟了', 'vague'), NOW+5)
        reply = self.followups.handle(job('面试改期到后天下午3点', 'clear'), NOW+10)
        self.assertIn('2026-11-03 17:00', reply or '')
        self.assertEqual(len(self.rows()), 1)
        self.followups.handle(job('同意调整跟进', 'confirm'), NOW+15)
        self.assertEqual([r['state'] for r in self.rows()], ['cancelled', 'pending'])

    def test_expired_and_quoted_fact_cannot_cancel_current_question(self):
        self.arrange()
        for n, text in enumerate(('如果面试取消了', '他说面试取消了', '我朋友的面试取消了',
                                  '我的面试可能改期到后天下午3点')):
            self.assertIsNone(self.followups.handle(job(text, 'bad'+str(n)), NOW+5))
        self.followups.handle(job('面试取消了', 'stale'), NOW+601)
        self.assertEqual(self.rows()[0]['state'], 'pending')

    def test_task_limit_does_not_consume_change_consent_or_restore_old_task(self):
        self.arrange()
        self.followups.handle(job('面试改期到后天下午3点', 'change'), NOW+5)
        for n in range(20):
            self.tasks.handle(job('添加待办：事项', 'todo'+str(n)), NOW+6)
        confirm = job('同意调整跟进', 'confirm')
        self.assertNotIn('已创建', self.followups.handle(confirm, NOW+10) or '')
        self.assertEqual(self.rows()[0]['state'], 'cancelled')
        self.tasks.handle(job('取消任务 '+self.rows()[1]['id'], 'free'), NOW+11)
        self.assertIn('已创建', self.followups.handle(confirm, NOW+12) or '')
        self.assertEqual(len(self.rows()), 22)

    def test_concurrent_confirmations_create_only_one_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            stores = [Store(os.path.join(directory, 'state.db')) for _ in range(2)]
            try:
                followups = [Followups(store) for store in stores]
                followups[0].handle(job('明天下午3点面试结束后问问我', 'original'), NOW)
                followups[0].handle(job('面试改期到后天下午3点', 'change'), NOW+5)
                barrier = threading.Barrier(2)
                def before_insert(statement):
                    if statement.startswith('INSERT OR IGNORE INTO tasks '):
                        barrier.wait(timeout=5)
                for store in stores:
                    store._conn.set_trace_callback(before_insert)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(f.handle, job('同意调整跟进', 'confirm'+str(n)), NOW+10)
                               for n, f in enumerate(followups)]
                    replies = [f.result(timeout=10) for f in futures]
                self.assertEqual(len(stores[0]._rows('SELECT * FROM tasks')), 2)
                self.assertEqual(sum('已创建' in (reply or '') for reply in replies), 1)
            finally:
                for store in stores:
                    store.close()

    def test_generic_task_commands_resolve_followup_holds_and_preserve_unknown(self):
        first = self.arrange()
        second = self.arrange('后天上午9点面试结束后问问我', 'second')
        self.followups.handle(job('面试推迟了', 'change'), NOW+5)
        reply = self.followups.handle(job('取消任务 '+first['id'], 'cancel'), NOW+10)
        self.assertIn('已取消', reply or '')
        self.assertEqual(self.rows()[0]['state'], 'cancelled')
        self.assertTrue(self.followups.can_deliver(second, NOW+47*3600))
        self.tasks.claim(second['id'])
        self.tasks.finish(second['id'], 'unconfirmed')
        for n, command in enumerate(('取消任务 ', '完成任务 ')):
            reply = self.followups.handle(job(command+second['id'], 'unknown'+str(n)), NOW+15)
            self.assertIn('未确认', reply or '')
            self.assertEqual(self.rows()[1]['state'], 'unconfirmed')
        self.tasks.handle(job('添加待办：事项', 'ordinary'), NOW+20)
        self.assertIsNone(self.followups.handle(job('取消任务 '+self.rows()[2]['id'], 'pass-through'), NOW+25))


if __name__ == '__main__':
    unittest.main()
