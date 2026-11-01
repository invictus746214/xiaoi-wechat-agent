import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from conversation_memory import People, group_transcript
from store import Store


ZONE = dt.timezone(dt.timedelta(hours=8))


def stamp(day, hour=12):
    return dt.datetime(2026, 11, day, hour, tzinfo=ZONE).timestamp()


class TopicTemporalTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.people = People(self.store)

    def tearDown(self):
        self.store.close()

    def record(self, n, text, scene='room@chatroom', sender='alice', when=None, **extra):
        row = dict(local_id=n, text=text, sender=sender, type=1, outgoing=False,
                   timestamp=stamp(1) if when is None else when, **extra)
        self.people.record('self', scene, '合成群', [row], 'synthetic.db',
                           {'alice': '甲', 'bob': '乙'})

    def test_old_arrangement_retrieval_is_attributed_and_scene_account_scoped(self):
        self.record(1, '我们下周六下午三点在公园碰面')
        for n in range(2, 90):
            self.record(n, '今天的闲聊内容'+str(n), when=stamp(2))
        value = self.people.topic_context('self', 'room@chatroom', '公园碰面几点', now=stamp(3))
        self.assertIn('公园碰面', value)
        self.assertIn('synthetic.db:1', value)
        self.assertIn('2026-11-01', value)
        self.assertIn('甲#', value)
        self.assertEqual(self.people.topic_context('self', 'other@chatroom', '公园', now=stamp(3)), '')
        self.assertEqual(self.people.topic_context('other', 'room@chatroom', '公园', now=stamp(3)), '')

    def test_temporary_facts_expire_without_reappearing_in_raw_recall(self):
        self.record(1, '我今天在深圳')
        live = self.people.context('self', 'alice', 'room@chatroom', '在哪', now=stamp(1, 23))
        self.assertIn('深圳', live)
        self.assertIn('2026-11-02', live)
        for now in (stamp(2, 0), stamp(3)):
            self.assertNotIn('深圳', self.people.context('self', 'alice', 'room@chatroom', '深圳', now=now))
            self.assertNotIn('深圳', self.people.topic_context('self', 'room@chatroom', '深圳', now=now))
        self.assertNotIn('深圳', self.people.context('self', 'alice', 'alice', '在哪', now=stamp(1)))

    def test_newer_correction_replaces_old_fact_even_after_out_of_order_replay(self):
        self.record(2, '我这周在杭州', when=stamp(2))
        self.record(1, '我这周在深圳', when=stamp(1))
        value = self.people.context('self', 'alice', 'room@chatroom', '在哪', now=stamp(3))
        self.assertIn('杭州', value)
        self.assertNotIn('深圳', value)
        topic = self.people.topic_context('self', 'room@chatroom', '深圳杭州', now=stamp(3))
        self.assertNotIn('深圳', topic)

    def test_arrangement_correction_and_cancellation_do_not_keep_old_plan(self):
        self.record(1, '我们下周六下午三点在公园碰面')
        self.record(2, '原定公园碰面改成下周日下午四点在咖啡馆', when=stamp(2))
        value = self.people.topic_context('self', 'room@chatroom', '碰面', now=stamp(3))
        self.assertIn('咖啡馆', value)
        self.assertNotIn('synthetic.db:1"', value)
        self.record(3, '下周的碰面取消了', when=stamp(3))
        value = self.people.topic_context('self', 'room@chatroom', '碰面', now=stamp(3))
        self.assertIn('取消', value)
        self.assertNotIn('咖啡馆', value)

    def test_question_and_addressee_are_preserved_without_inventing_an_answer(self):
        self.record(1, '@乙 下周聚餐在哪？')
        self.record(2, '我也不知道', sender='bob', quote_sender='alice', quote_text='@乙 下周聚餐在哪？')
        value = self.people.topic_context('self', 'room@chatroom', '聚餐在哪', now=stamp(2))
        self.assertIn('未确认已解决', value)
        self.assertIn('@乙', value)
        self.assertIn('乙#', value)
        self.assertIn('甲#', value)
        self.assertIn('我也不知道', value)

    def test_forgetting_covers_derived_topics_and_replay_but_other_people_remain(self):
        self.record(1, '我下周要面试')
        self.record(2, '我下周要考试', sender='bob')
        self.record(3, '忘记关于我的所有记忆', when=stamp(2))
        self.record(11, '我下周要面试', when=stamp(1))
        value = self.people.topic_context('self', 'room@chatroom', '面试考试', now=stamp(3))
        self.assertNotIn('面试', value)
        self.assertIn('考试', value)

    def test_interest_forgetting_removes_derived_topic(self):
        self.record(1, '我喜欢猫')
        self.record(2, '忘记我喜欢猫', when=stamp(2))
        self.assertNotIn('喜欢猫', self.people.topic_context('self', 'room@chatroom', '猫', now=stamp(3)))

    def test_forget_removes_other_speakers_explicit_quote_dependencies(self):
        self.record(1, '我下周要面试')
        self.record(2, '你的面试记得准备', sender='bob', quote_sender='alice', quote_text='我下周要面试')
        self.record(3, '忘记关于我的所有记忆', when=stamp(2))
        self.assertNotIn('面试', self.people.topic_context('self', 'room@chatroom', '面试', now=stamp(3)))
        self.record(4, '你的面试记得准备', sender='bob', when=stamp(3),
                    quote_sender='alice', quote_text='我下周要面试')
        self.assertNotIn('面试', self.people.topic_context('self', 'room@chatroom', '面试', now=stamp(3)))

    def test_focus_time_does_not_receive_future_batch_or_future_correction(self):
        self.record(1, '我这周在深圳', when=stamp(2))
        self.record(2, '我这周在杭州', when=stamp(3))
        self.assertIn('深圳', self.people.context('self', 'alice', 'room@chatroom', '在哪', now=stamp(2)))
        self.assertNotIn('杭州', self.people.topic_context('self', 'room@chatroom', '在哪', now=stamp(2)))

    def test_exact_date_week_and_unspecified_temporary_expiry(self):
        cases = [('我2026年11月4日要面试', stamp(4), stamp(5)),
                 ('我这周在深圳', stamp(1, 23), stamp(2, 0)),
                 ('我最近在准备考试', stamp(14), stamp(16))]
        for n, (text, live, expired) in enumerate(cases, 1):
            with self.subTest(text=text):
                scene='scene'+str(n)+'@chatroom'
                self.record(n, text, scene=scene)
                self.assertIn(text, self.people.context('self', 'alice', scene, '之前说过', now=live))
                self.assertNotIn(text, self.people.context('self', 'alice', scene, '之前说过', now=expired))

    def test_current_activity_without_end_date_is_not_kept_forever(self):
        self.record(1, '我在准备考试')
        self.assertIn('我在准备考试', self.people.context('self', 'alice', 'room@chatroom', '考试', now=stamp(2)))
        self.assertNotIn('我在准备考试', self.people.context('self', 'alice', 'room@chatroom', '考试', now=stamp(16)))

    def test_unanswered_query_can_find_old_question_after_many_new_topics(self):
        self.record(1, '@乙 小组设备什么时候能到？')
        for n in range(2, 160):
            self.record(n, '新问题相关的随口聊天'+str(n), when=stamp(2))
        value=self.people.topic_context('self', 'room@chatroom', '还有哪些未回答的问题', now=stamp(3))
        self.assertIn('设备什么时候能到', value)

    def test_restart_preserves_expiry_correction_and_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'synthetic.db')
            store = Store(path)
            try:
                people = People(store)
                people.record('self', 'room@chatroom', '合成', [dict(local_id=1, text='我今天在深圳',
                    sender='alice', type=1, timestamp=stamp(1))], 'synthetic.db', {'alice':'甲'})
            finally:
                store.close()
            store = Store(path)
            try:
                people = People(store)
                self.assertIn('深圳', people.context('self', 'alice', 'room@chatroom', '在哪', now=stamp(1)))
                self.assertNotIn('深圳', people.topic_context('self', 'room@chatroom', '在哪', now=stamp(2)))
            finally:
                store.close()

    def test_purge_removes_topic_sources_and_metadata(self):
        self.record(1, '老安排在公园碰面', when=1)
        self.people.purge(14)
        self.assertEqual(self.people.topic_context('self', 'room@chatroom', '公园'), '')

    def transcript(self, rows, now=None):
        job=dict(account='self', username='room@chatroom',
                 batch=[dict(source='synthetic.db', message=rows[-1])])
        if now is not None:
            job['focus']=dict(message=dict(timestamp=now))
        reader=SimpleNamespace(messages=lambda *args, **kwargs: rows)
        return group_transcript(reader, job, {'alice':'甲','bob':'乙'}, self.people)

    def test_raw_transcript_filters_expired_sources_even_without_index(self):
        for indexed in (False, True):
            with self.subTest(indexed=indexed):
                if indexed:
                    self.record(1, '我今天在深圳')
                rows=[dict(local_id=1, text='我今天在深圳', sender='alice', timestamp=stamp(1)),
                      dict(local_id=2, text='现在聊什么', sender='bob', timestamp=stamp(2))]
                value=self.transcript(rows)
                self.assertNotIn('深圳', value)
                self.assertIn('现在聊什么', value)
                self.assertIn('2026-11-02', value)
                self.assertIn('+08:00', value)

    def test_raw_transcript_excludes_superseded_source_and_future_correction(self):
        self.record(1, '我这周在深圳', when=stamp(2))
        self.record(2, '我这周在杭州', when=stamp(3))
        rows=[dict(local_id=1, text='我这周在深圳', sender='alice', timestamp=stamp(2)),
              dict(local_id=2, text='我这周在杭州', sender='alice', timestamp=stamp(3))]
        value=self.transcript(rows, now=stamp(3))
        self.assertNotIn('深圳', value)
        self.assertIn('杭州', value)
        value=self.transcript(rows, now=stamp(2))
        self.assertIn('深圳', value)
        self.assertNotIn('杭州', value)

    def test_raw_transcript_does_not_reintroduce_forgotten_other_speaker_quotes(self):
        self.record(1, '我下周要面试')
        self.record(2, '忘记关于我的所有记忆', when=stamp(2))
        rows=[dict(local_id=1, text='我下周要面试', sender='alice', timestamp=stamp(1)),
              dict(local_id=3, text='记得准备', sender='bob', timestamp=stamp(3),
                   quote_sender='alice', quote_text='我下周要面试')]
        self.assertNotIn('面试', self.transcript(rows))
        self.assertNotIn('记得准备', self.transcript(rows))

    def test_raw_transcript_drops_quoted_expired_and_corrected_source(self):
        self.record(1, '我今天在深圳')
        rows=[dict(local_id=2, text='那边天气如何', sender='bob', timestamp=stamp(2),
                   quote_sender='alice', quote_text='我今天在深圳')]
        self.assertNotIn('深圳', self.transcript(rows))
        self.record(3, '我这周在杭州', when=stamp(2))
        self.record(4, '我这周在北京', when=stamp(3))
        rows=[dict(local_id=5, text='那边天气如何', sender='bob', timestamp=stamp(3),
                   quote_sender='alice', quote_text='我这周在杭州')]
        self.assertNotIn('杭州', self.transcript(rows))

    def test_quote_with_ambiguous_original_date_cannot_rebase_expired_today(self):
        self.record(1, '我今天在深圳', when=stamp(1))
        self.record(2, '我今天在深圳', when=stamp(2))
        quote=dict(local_id=3, text='那里怎么样', sender='bob', timestamp=stamp(2),
                   quote_sender='alice', quote_text='我今天在深圳')
        self.assertNotIn('深圳', self.transcript([quote]))
        quote['quote_timestamp']=stamp(2)
        self.assertIn('深圳', self.transcript([quote]))

    def test_public_validity_and_legacy_guard_do_not_bypass_source_rules(self):
        self.assertTrue(self.people.allow_legacy('self', 'alice', 'alice'))
        self.record(1, '我今天在深圳', scene='alice')
        source=dict(local_id=1, text='我今天在深圳', sender='alice', timestamp=stamp(1))
        self.assertTrue(self.people.source_valid('self', 'alice', 'synthetic.db', source, now=stamp(1)))
        self.assertFalse(self.people.source_valid('self', 'alice', 'synthetic.db', source, now=stamp(2)))
        self.assertFalse(self.people.allow_legacy('self', 'alice', 'alice'))
        self.assertTrue(self.people.allow_legacy('self', 'alice', 'unrelated'))

    def test_unrelated_same_event_cancellation_does_not_erase_future_plan(self):
        self.record(1, '我们下周六在公园碰面', when=stamp(1))
        self.record(2, '今天公司的碰面取消了', when=stamp(2))
        value=self.people.topic_context('self', 'room@chatroom', '下周六公园碰面', now=stamp(3))
        self.assertIn('下周六在公园碰面', value)
        raw=[dict(local_id=1, text='我们下周六在公园碰面', sender='alice', timestamp=stamp(1)),
             dict(local_id=3, text='问下周的计划', sender='bob', timestamp=stamp(3))]
        self.assertIn('公园碰面', self.transcript(raw))

    def test_ambiguous_reschedule_keeps_both_attributed_plans(self):
        self.record(1, '我们下周六在公园碰面')
        self.record(2, '改成下周日在咖啡馆碰面', when=stamp(2))
        value=self.people.topic_context('self', 'room@chatroom', '碰面', now=stamp(3))
        self.assertIn('公园', value)
        self.assertIn('咖啡馆', value)

    def assistant(self, text, when, role='assistant', key='private'):
        self.store._write('INSERT INTO messages(session_key,role,text,ts) VALUES(?,?,?,?)',
                          (key, role, text, when))

    def test_safe_assistant_history_is_recent_same_session_and_after_expiry(self):
        now=stamp(2, 0)+300
        self.record(1, '我今天在深圳', scene='alice')
        self.assistant('旧地点回声', stamp(2, 0)-60)
        self.assistant('保留最近的语气', stamp(2, 0)+120)
        self.assistant('另一个会话', now-30, key='other')
        self.assistant('未来回复', now+30)
        self.assistant('用户旧话', now-10, role='user')
        self.assertEqual(self.people.safe_assistant_history('self', 'alice', 'alice', 'private', now=now),
                         [('assistant', '保留最近的语气')])

    def test_safe_assistant_history_drops_echo_before_correction_and_forget(self):
        now=stamp(3)
        self.record(1, '我这周在深圳', scene='alice', when=now-500)
        self.assistant('去深圳的旧回复', now-200)
        self.record(2, '我这周在杭州', scene='alice', when=now-100)
        self.assistant('短语气延续', now-50)
        self.assertEqual(self.people.safe_assistant_history('self', 'alice', 'alice', 'private', now=now),
                         [('assistant', '短语气延续')])
        self.record(3, '忘记关于我的所有记忆', scene='alice', when=now-25)
        self.assertEqual(self.people.safe_assistant_history('self', 'alice', 'alice', 'private', now=now), [])

    def test_safe_assistant_history_drops_older_than_ten_minutes_and_honors_limit(self):
        now=stamp(3)
        self.assistant('太旧', now-601)
        self.assistant('较早', now-200)
        self.assistant('较新', now-100)
        self.assertEqual(self.people.safe_assistant_history('self', 'alice', 'alice', 'private', now=now, limit=1),
                         [('assistant', '较新')])

    def test_group_assistant_echo_before_any_account_forget_is_not_reintroduced(self):
        self.record(1, '忘记关于我的所有记忆', sender='alice', when=stamp(2))
        rows=[dict(local_id=2, text='你之前说下周面试', sender='self', timestamp=stamp(1), outgoing=True),
              dict(local_id=3, text='其他人的原话', sender='bob', timestamp=stamp(1)),
              dict(local_id=4, text='最近的新话题', sender='bob', timestamp=stamp(3))]
        value=self.transcript(rows)
        self.assertNotIn('面试', value)
        self.assertIn('其他人的原话', value)

    def test_private_assistant_echo_cutoff_respects_another_person_forget(self):
        now=stamp(3)
        self.assistant('引用乙之前的话', now-200)
        self.record(1, '忘记关于我的所有记忆', scene='bob', sender='bob', when=now-100)
        self.assistant('遗忘后的语气延续', now-50)
        self.assertEqual(self.people.safe_assistant_history('self', 'alice', 'alice', 'private', now=now),
                         [('assistant', '遗忘后的语气延续')])


if __name__ == '__main__':
    unittest.main()
