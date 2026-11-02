import hashlib
import os
import tempfile
import unittest
from datetime import datetime

from store import Store
from tasks import CN, Tasks

from followups import Followups


NOW = datetime(2026, 11, 1, 10, 0, tzinfo=CN).timestamp()


def job(text, event='e1', person='alice', account='bot'):
    local_id = int(hashlib.sha256(event.encode()).hexdigest()[:8], 16)+1
    native_event = hashlib.sha256(f'{account}\0{person}\0message_0.db\0{local_id}'.encode()).hexdigest()[:32]
    return dict(account=account, person_id=person, username=person, title='朋友',
                session_key=account + ':' + person, kind='private', event_id=native_event,
                focus=dict(event_id=native_event, source='message_0.db',
                           message=dict(local_id=local_id, timestamp=NOW, text=text,
                                        sender=person, type=1, outgoing=False)))


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Followups, 'consented followup feature is missing')
        self.store = Store()
        self.followups = Followups(self.store)
        self.tasks = Tasks(self.store)

    def tearDown(self):
        if hasattr(self, 'store'):
            self.store.close()

    def rows(self):
        return self.store._rows('SELECT * FROM tasks ORDER BY created_at,id')

    def test_proposal_has_no_active_task_until_explicit_consent(self):
        offer = self.followups.observe(job('我明天下午3点有面试'), NOW)
        self.assertIn('问问', offer)
        self.assertEqual(self.rows(), [])
        self.assertIsNone(self.followups.handle(job('好的', 'small-talk'), NOW+5))
        self.assertEqual(self.rows(), [])
        receipt = self.followups.handle(job('好，之后问问我', 'consent'), NOW+10)
        row = self.rows()[0]
        self.assertIn(row['id'], receipt)
        self.assertIn('2026-11-02 17:00', receipt)
        self.assertEqual(row['due_at'], NOW + 31*3600)
        self.assertEqual(row['state'], 'pending')
        self.assertEqual(self.followups.delivery_text(row), '之前提到的面试进展怎么样？')
        self.assertEqual(self.followups.handle(job('好，之后问问我', 'consent'), NOW+20), receipt)
        self.assertEqual(len(self.rows()), 1)

    def test_one_step_explicit_request_uses_task_delivery_and_cancellation(self):
        receipt = self.followups.handle(job('明天下午3点面试结束后问问我进展'), NOW)
        row = self.rows()[0]
        self.assertIn(row['id'], receipt)
        self.assertEqual(row['due_at'], NOW+31*3600)
        self.assertIn(row['id'], self.tasks.handle(job('查看任务', 'list'), NOW))
        self.assertIn('已取消', self.tasks.handle(job('取消任务 '+row['id'], 'cancel'), NOW))
        self.assertEqual(self.tasks.due('bot', NOW+31*3600+1), [])
        self.assertIsNone(self.followups.delivery_text(dict(row, id='ordinary')))

    def test_followup_specific_listing_and_cancel_remain_private(self):
        self.followups.handle(job('明天下午3点面试结束后问问我进展'),NOW)
        row=self.rows()[0]
        self.assertIn(row['id'],self.followups.handle(job('查看跟进','list'),NOW))
        self.assertNotIn(row['id'],self.followups.handle(job('查看跟进','other-list',person='bob'),NOW))
        self.assertIn('已取消',self.followups.handle(job('取消跟进 '+row['id'],'cancel-followup'),NOW))
        self.assertEqual(self.rows()[0]['state'],'cancelled')

    def test_cancel_unknown_followup_does_not_erase_delivery_uncertainty(self):
        self.followups.handle(job('明天下午3点面试结束后问问我进展'),NOW)
        row=self.rows()[0];self.tasks.claim(row['id']);self.tasks.finish(row['id'],'unconfirmed')
        self.assertIn('未确认',self.followups.handle(job('取消跟进 '+row['id'],'cancel-unknown'),NOW))
        self.assertEqual(self.rows()[0]['state'],'unconfirmed')

    def test_unconsented_expired_proposal_is_purged(self):
        self.followups.observe(job('明天下午3点面试'),NOW)
        self.followups.purge(NOW+601)
        self.assertEqual(self.store._rows('SELECT * FROM followup_proposals'),[])

    def test_consent_is_scoped_to_owner_scene_account_and_expires(self):
        self.followups.observe(job('明天下午3点面试'), NOW)
        for request in (job('可以，结束后问问我', 'c2', person='bob'),
                        job('可以，结束后问问我', 'c3', account='other')):
            self.assertNotIn('已创建', self.followups.handle(request, NOW+10) or '')
        expired = job('可以，结束后问问我', 'expired')
        expired['focus']['message']['timestamp'] = NOW+601
        self.assertNotIn('已创建', self.followups.handle(expired, NOW+601))
        self.assertEqual(self.rows(), [])

    def test_rejects_non_native_sources_and_quoted_instructions(self):
        for index, mutate in enumerate((
                lambda j: j.update(kind='group', username='g@chatroom'),
                lambda j: j['focus']['message'].update(outgoing=True),
                lambda j: j['focus']['message'].update(type=34),
                lambda j: j['focus']['message'].update(sender='bob'),
                lambda j: j['focus']['message'].update(quote_sender='bob'),
                lambda j: j['focus']['message'].update(quote_text='hello'),
                lambda j: j['focus'].update(source=''),
                lambda j: j['focus']['message'].update(local_id=0),
                lambda j: j['focus'].update(event_id=''),
                lambda j: j['focus'].update(event_id='f'*32),
                lambda j: j['focus']['message'].update(timestamp=float('nan')),
                lambda j: j['focus']['message'].update(timestamp=NOW+61))):
            request = job('明天下午3点面试结束后问问我进展', str(index))
            mutate(request)
            self.assertIsNone(self.followups.handle(request, NOW))
            self.assertIsNone(self.followups.observe(request, NOW))
        for text in ('他说“明天下午3点面试结束后问问我进展”',
                     '明天下午3点面试，“结束后问问我进展”',
                     '如果明天下午3点面试结束后问问我进展',
                     '明天下午3点不要面试结束后问问我进展',
                     '转发：明天下午3点面试结束后问问我进展',
                     '明天下午3点李四的面试结束后问问我进展',
                     '明天下午3点面试结束后问问我进展，给@小明发消息'):
            self.assertFalse(self.followups.handle(job(text), NOW))
            self.assertFalse(self.followups.observe(job(text), NOW))
        self.assertEqual(self.rows(), [])

    def test_same_event_modified_topic_does_not_change_persisted_offer(self):
        first = self.followups.observe(job('明天下午3点面试'), NOW)
        self.assertEqual(self.followups.observe(job('后天上午9点考试'), NOW+10), first)

    def test_ambiguous_calendar_requests_need_specific_future_time(self):
        for text in ('明天面试结束后问问我进展', '周一下午3点面试结束后问问我进展',
                     '明天晚上12点面试结束后问问我进展', '今天上午9点面试结束后问问我进展',
                     '明天下午13点面试结束后问问我进展'):
            response = self.followups.handle(job(text), NOW)
            self.assertIn('具体', response)
        self.assertEqual(self.rows(), [])

    def test_explicit_consent_time_overrides_default_and_anchors_to_source(self):
        self.followups.observe(job('明天下午3点面试'), NOW)
        receipt = self.followups.handle(job('可以，后天上午9点问问我进展', 'consent'), NOW+30)
        self.assertIn('2026-11-03 09:00', receipt)
        self.assertEqual(self.rows()[0]['due_at'], NOW+47*3600)

    def test_duplicate_proposals_do_not_refresh_consent_window(self):
        request = job('明天下午3点面试')
        self.followups.observe(request, NOW)
        self.followups.observe(request, NOW+590)
        consent = job('好，之后问问我', 'consent')
        consent['focus']['message']['timestamp'] = NOW+601
        self.assertNotIn('已创建', self.followups.handle(consent, NOW+601))
        self.assertEqual(self.rows(), [])

    def test_restart_and_redelivery_do_not_create_second_followup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'followups.db')
            store = Store(path)
            followups = Followups(store)
            followups.observe(job('明天下午3点面试'), NOW)
            store.close()
            store = Store(path)
            try:
                followups = Followups(store)
                receipt = followups.handle(job('好，之后问问我', 'consent'), NOW+10)
                followups.observe(job('后天上午9点考试', 'next-event'), NOW+20)
                self.assertEqual(followups.handle(job('好，之后问问我', 'consent'), NOW+30), receipt)
                tasks = Tasks(store)
                row = store._rows('SELECT * FROM tasks')[0]
                self.assertTrue(tasks.claim(row['id']))
                self.assertEqual(tasks.recover(), 1)
                self.assertEqual(tasks.due('bot', NOW+31*3600+1), [])
                self.assertEqual(len(store._rows('SELECT * FROM tasks')), 1)
            finally:
                store.close()

    def test_task_cap_does_not_claim_success_or_consume_proposal(self):
        for index in range(20):
            self.tasks.handle(job('添加待办：事项', 'todo'+str(index)), NOW)
        self.followups.observe(job('明天下午3点面试'), NOW)
        request = job('好，之后问问我', 'consent')
        self.assertNotIn('已创建', self.followups.handle(request, NOW+10))
        self.assertEqual(len(self.rows()), 20)
        self.tasks.handle(job('取消任务 '+self.rows()[0]['id'], 'cancel'), NOW+11)
        self.assertIn('已创建', self.followups.handle(request, NOW+12))


if __name__ == '__main__':
    unittest.main()
