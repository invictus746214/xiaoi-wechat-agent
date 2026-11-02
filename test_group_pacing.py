"""Source-grounded group turns and nonblocking, durable burst pacing."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from conversation_memory import group_focus, group_transcript, group_turn, remember_group_reply
from event_pipeline import Inbox


def message(n, text='hello', stamp=100, sender='a', kind=1, **extra):
    return dict(local_id=n, text=text, timestamp=stamp, sender=sender,
                type=kind, outgoing=False, **extra)


class PacingTests(unittest.TestCase):
    def setUp(self):
        self.inbox = Inbox(':memory:')
        self.addCleanup(self.inbox.close)

    def ingest(self, rows, at=100, user='a', priority=4):
        with patch('event_pipeline.time.time', return_value=at):
            self.inbox.ingest('self', user, 's', user, rows, priority)

    def claim(self, now, **extra):
        return self.inbox.claim(set(), now=now, quiet_seconds=1.2, max_wait_seconds=4, **extra)

    def test_text_burst_waits_until_last_arrival_quiet_then_combines(self):
        self.ingest([message(1)], at=100)
        self.assertIsNone(self.claim(100.1))
        self.ingest([message(2, 'second', stamp=101)], at=100.8)
        self.assertIsNone(self.claim(101.3))
        self.assertEqual([r['local_id'] for r in self.claim(102.1)], [1, 2])

    def test_continuous_arrivals_cannot_extend_original_head_deadline(self):
        for n in range(1, 5):
            self.ingest([message(n, stamp=99+n)], at=99+n)
        self.assertIsNone(self.claim(103.1))
        self.assertEqual(len(self.claim(104)), 4)

    def test_waiting_private_does_not_hold_ready_other_chat(self):
        self.ingest([message(1, stamp=80)], at=100, user='room@chatroom', priority=1)
        self.ingest([message(1)], at=100)
        self.assertEqual(self.claim(100.1)[0]['username'], 'room@chatroom')
        self.assertEqual(self.inbox.count(), 2)

    def test_mentions_stop_and_predicate_bypass_without_sleep(self):
        for n, text in enumerate(('@小i 帮我看看', '停止', '运行状态', '管理员指令'), 1):
            with self.subTest(text=text):
                self.ingest([message(n, text)], user='friend'+str(n))
                batch=self.claim(100.1, bypass_wait=lambda m: m['text']=='管理员指令')
                self.assertEqual(batch[0]['message']['text'], text)
                self.inbox.complete(batch, 'done', now=100.1)

    def test_later_stop_bypasses_wait_for_the_whole_unclaimed_burst(self):
        self.ingest([message(1), message(2, '别发了', stamp=101)], at=100)
        self.assertEqual(len(self.claim(100.1)), 2)

    def test_picture_and_sticker_captions_still_coalesce(self):
        for kind in (3, 47):
            with self.subTest(kind=kind):
                user='friend'+str(kind)
                self.ingest([message(1, '', kind=kind)], user=user)
                self.assertIsNone(self.claim(100.1))
                self.ingest([message(2, '这张怎么样', stamp=101)], at=100.8, user=user)
                batch=self.claim(102.1)
                self.assertEqual([r['message']['type'] for r in batch], [kind, 1])
                self.inbox.complete(batch, 'done', now=102.1)

    def test_stale_historical_messages_do_not_acquire_new_wait_on_ingest(self):
        for index,stamp in enumerate((10,95),1):
            with self.subTest(stamp=stamp):
                self.ingest([message(index, stamp=stamp)], at=100,user='old'+str(index))
                batch=self.claim(100.1)
                self.assertIsNotNone(batch)
                self.inbox.complete(batch,'done',now=100.1)

    def test_quoted_stop_body_is_not_an_urgent_source_instruction(self):
        self.ingest([message(1,'这是什么意思',quote_sender='b',quote_text='停止')])
        self.assertIsNone(self.claim(100.1))

    def test_future_source_clock_uses_actual_arrival_and_still_has_a_cap(self):
        self.ingest([message(1, stamp=10000)], at=100)
        self.assertIsNone(self.claim(100.1))
        self.assertIsNotNone(self.claim(104))

    def test_saved_retry_retains_boundary_and_does_not_wait_for_new_arrival(self):
        self.ingest([message(1)], at=100)
        batch=self.inbox.claim(set(), now=100)
        self.inbox.save_reply(batch, 'frozen')
        self.inbox.complete(batch, 'retry', now=100)
        self.ingest([message(2, stamp=106)], at=106)
        retried=self.claim(106.1)
        self.assertEqual([r['local_id'] for r in retried], [1])
        self.assertEqual(retried[0]['reply'], 'frozen')
        self.inbox.complete(retried, 'unconfirmed', now=106.1)
        self.assertIsNone(self.claim(106.2))
        self.assertEqual([r['local_id'] for r in self.claim(108)], [2])

    def test_group_speaker_change_retains_original_batch_boundary(self):
        self.ingest([message(1), message(2, sender='b')], at=100, user='room@chatroom')
        batch=self.claim(100.1)
        self.assertEqual([r['message']['sender'] for r in batch], ['a'])

    def test_legacy_saved_batch_with_mixed_speakers_retains_retry_boundary(self):
        self.ingest([message(1),message(2,sender='b')],user='room@chatroom')
        with self.inbox.db:
            self.inbox.db.execute("UPDATE inbox SET batch_id='legacy',reply='frozen'")
        batch=self.claim(100.1)
        self.assertEqual([r['local_id'] for r in batch],[1,2])

    def test_pending_pacing_deadline_survives_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'state.db')
            inbox=Inbox(path)
            with patch('event_pipeline.time.time',return_value=100):
                inbox.ingest('self','a','s','a',[message(1)],4)
            inbox.close()
            inbox=Inbox(path)
            try:
                self.assertIsNone(inbox.claim(set(),now=100.5,quiet_seconds=1.2))
                self.assertIsNotNone(inbox.claim(set(),now=101.3,quiet_seconds=1.2))
            finally:inbox.close()


class GroupDirectionTests(unittest.TestCase):
    def setUp(self):
        self.engaged={('room@chatroom','a'):200}
        self.topics={('room@chatroom','a'):{'显卡'}}
        self.policy={'group_engaged_seconds':120}
        self.job=dict(kind='group', username='room@chatroom', identity='room@chatroom',
                      person_id='a', focus_text='你觉得呢', is_mention=False,
                      sender_names={'self':'小i','a':'甲','b':'小林'})

    def turn(self, job=None):
        return group_turn(job or self.job, 'self', self.policy, bridge,
                          self.engaged, self.topics, 100)

    def test_known_name_direct_address_is_not_answered_for_another_person(self):
        self.assertEqual(self.turn(dict(self.job,focus_text='小林，你觉得呢')), (False,False))

    def test_addressing_others_ends_old_implicit_engagement(self):
        self.assertEqual(self.turn(dict(self.job,focus_text='@小林 显卡怎么样')), (False,False))
        self.assertEqual(self.turn(), (False,True))

    def test_quote_to_bot_and_explicit_mention_override_other_background(self):
        self.assertEqual(self.turn(dict(self.job,quote_sender='self')), (True,False))
        self.assertEqual(self.turn(dict(self.job,is_mention=True,quote_sender='b')), (True,False))

    def test_reply_generated_topics_never_create_new_followup_subject(self):
        remember_group_reply(dict(self.job,focus_text='显卡多少钱'),'要不要去海边度假',self.policy,
                             bridge,self.engaged,self.topics,{},90)
        self.assertNotIn('海边',self.topics['room@chatroom','a'])
        self.assertEqual(self.turn(dict(self.job,focus_text='海边天气怎么样')), (False,True))

    def test_opaque_media_text_does_not_seed_an_unobserved_group_topic(self):
        job=dict(self.job,focus_text='海边',batch=[dict(source='s',message=message(1,'海边',kind=47))])
        remember_group_reply(job,'[表情]',self.policy,bridge,self.engaged,self.topics,{},90)
        self.assertEqual(self.topics['room@chatroom','a'],set())

    def test_empty_source_topic_does_not_make_every_message_an_engaged_followup(self):
        self.topics['room@chatroom','a']=set()
        self.assertEqual(self.turn(dict(self.job,focus_text='今天中午吃什么')), (False,True))
        self.assertEqual(self.turn(), (True,False))

    def test_recent_quoted_conversation_with_other_member_suppresses_ambiguous_followup(self):
        recent=[dict(source='s',local_id=1,sender='b',text='显卡你来选',quote_sender='a',timestamp=99)]
        self.assertEqual(self.turn(dict(self.job,group_recent_sources=recent)), (False,False))

    def test_recent_explicit_named_conversation_is_not_an_implicit_bot_followup(self):
        recent=[dict(source='s',local_id=1,sender='b',text='@甲 显卡你来选',timestamp=99)]
        self.assertEqual(self.turn(dict(self.job,group_recent_sources=recent)), (False,False))

    def test_unrelated_other_speaker_does_not_steal_current_followup(self):
        recent=[dict(source='s',local_id=1,sender='b',text='今天天气很好',timestamp=99)]
        self.assertEqual(self.turn(dict(self.job,group_recent_sources=recent)), (True,False))

    def test_unknown_media_payload_cannot_change_group_addressee(self):
        rows=[message(1,'@甲',99,sender='b',kind=47),message(2,'你觉得呢',100)]
        class Reader:
            def messages(self,user,limit,source):return rows
        job=dict(self.job,account='self',batch=[dict(source='s',message=rows[1])],
                 focus=dict(source='s',message=rows[1]))
        group_transcript(Reader(),job,self.job['sender_names'])
        self.assertEqual(self.turn(job),(True,False))

    def test_transcript_exposes_only_bounded_source_turns_for_direction(self):
        rows=[message(1,'显卡你来选',99,sender='b',quote_sender='a'),message(2,'你觉得呢',100),
              message(3,'未来话题',101,sender='b')]
        class Reader:
            def messages(self, user, limit, source):return rows
        job=dict(self.job, account='self', batch=[dict(source='s',message=rows[1])],
                 focus=dict(source='s',message=rows[1]))
        transcript=group_transcript(Reader(),job,self.job['sender_names'])
        self.assertIn('group_recent_sources',job)
        self.assertEqual([r['local_id'] for r in job['group_recent_sources']], [1,2])
        self.assertNotIn('未来话题',transcript)
        self.assertEqual(self.turn(job), (False,False))
        focus=group_focus(dict(self.job,focus_text='小林，你觉得呢'),'self',self.job['sender_names'])
        self.assertIn('小林',focus)
        self.assertNotIn('明确对话对象：未明确',focus)


if __name__=='__main__':
    unittest.main()
