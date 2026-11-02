import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from sticker_library import StickerLibrary
from store import Store


class StickerCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store()
        self.library = StickerLibrary(self.store, self.temp.name, 'account')
        self.now = time.time()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add(self, scene='alice', sender='alice', local=1, seconds=10, state='pending', outgoing=False, account='account'):
        raw = b'GIF89a' + str((scene, sender, local)).encode()
        row = dict(source='message/message_0.db', message=dict(type=47, local_id=local,
            sender=sender, outgoing=outgoing, timestamp=self.now-seconds,
            sticker_hashes=[hashlib.md5(raw).hexdigest()]))
        identifier = self.library.collect_message(SimpleNamespace(account=account), scene, row,
            '小猫生气', raw=raw, tags=['生气'])
        if identifier:
            self.library.set_native(identifier, 'f'*32 if state=='confirmed' else '', state)
        return identifier, row

    def job(self, text, scene='alice', person='alice'):
        row = dict(source='message/message_0.db', message=dict(type=1, local_id=99,
            sender=person, outgoing=False, timestamp=self.now, text=text))
        return dict(account='account', username=scene, person_id=person, focus_text=text,
            message=text, focus=row, batch=[row], event_id='command')

    def handle(self, job):
        import sticker_commands
        return sticker_commands.handle(job, self.library, self.store, now=self.now)

    def test_status_counts_only_callers_recent_originals_and_distinguishes_unknown(self):
        for local, state in enumerate(('confirmed', 'pending', 'unconfirmed', 'unsupported'), 1):
            self.add(local=local, state=state)
        self.add(scene='bob', sender='bob', local=8)
        self.add(local=9, seconds=700)
        reply = self.handle(self.job('刚才哪些表情保存了'))
        self.assertIn('原文件已保存4', reply)
        self.assertIn('原生收藏已确认1', reply)
        self.assertIn('待处理1', reply)
        self.assertIn('结果未知1', reply)
        self.assertIn('不支持1', reply)
        self.assertNotIn('小猫', reply)
        self.assertNotIn('36', reply)

    def test_group_status_and_correction_bind_same_sender_and_scene(self):
        own, _ = self.add(scene='room@chatroom', seconds=20)
        other, _ = self.add(scene='room@chatroom', sender='bob', local=2, seconds=5)
        private, _ = self.add(local=3, seconds=2)
        reply = self.handle(self.job('表情保存状态', 'room@chatroom'))
        self.assertIn('原文件已保存1', reply)
        reply = self.handle(self.job('这张是无奈，不是生气', 'room@chatroom'))
        self.assertIn('无奈', reply)
        self.assertEqual(json.loads(self.library.get(own)['tags']), ['无奈'])
        self.assertNotIn('生气', self.library.get(own)['description'])
        self.assertEqual(json.loads(self.library.get(other)['tags']), ['生气'])
        self.assertEqual(json.loads(self.library.get(private)['tags']), ['生气'])

    def test_correction_preserves_unknown_native_outcome_and_only_latest_verified(self):
        older, _ = self.add(local=1, seconds=30)
        latest, _ = self.add(local=2, seconds=5, state='unconfirmed')
        before = self.library.get(latest)
        self.handle(self.job('把刚才的表情标成无奈'))
        after = self.library.get(latest)
        self.assertEqual(json.loads(after['tags']), ['无奈'])
        for key in ('native_state', 'native_md5', 'path', 'md5', 'bytes'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(json.loads(self.library.get(older)['tags']), ['生气'])

    def test_absent_foreign_legacy_or_unverified_sources_do_not_allow_correction(self):
        identifier, _ = self.add(scene='bob', sender='bob')
        reply = self.handle(self.job('这张是无奈，不是生气'))
        self.assertIn('无法确认', reply)
        self.assertEqual(json.loads(self.library.get(identifier)['tags']), ['生气'])
        self.assertIsNone(self.add(outgoing=True, local=4)[0])
        self.assertIsNone(self.add(account='other', local=5)[0])
        self.assertIsNotNone(self.add(scene='room@chatroom', sender='', local=6)[0])
        self.store._write('INSERT INTO sticker_sources VALUES(?,?,?,?,?,?)',
            ('account', identifier, 'alice', 'legacy', 7, self.now-1))
        self.assertIn('无法确认', self.handle(self.job('把刚才的表情标成无奈')))

    def test_original_collection_without_sender_keeps_legacy_behavior_but_no_command_evidence(self):
        identifier, _ = self.add(sender='', local=10)
        self.assertIsNotNone(identifier)
        self.assertEqual(len(self.store._rows('SELECT * FROM sticker_sources')), 1)
        self.assertEqual(self.store._rows('SELECT * FROM sticker_source_evidence'), [])
        self.assertIn('无法确认', self.handle(self.job('把刚才的表情标成无奈')))
        self.assertEqual(json.loads(self.library.get(identifier)['tags']), ['生气'])

    def test_continue_only_proposes_pending_without_replaying_unknown_or_sending(self):
        pending, _ = self.add(local=1, state='pending')
        unknown, _ = self.add(local=2, state='unconfirmed')
        self.library.record_send(pending, 'alice', 'old-send', 'sending')
        job = self.job('刚才没收藏的继续')
        reply = self.handle(job)
        self.assertIn('结果未知1', reply)
        self.assertEqual(job['sticker_continue_candidates'], [])
        self.assertTrue(job['sticker_continue_requested'])
        self.assertEqual(self.library.get(unknown)['native_state'], 'unconfirmed')
        self.assertEqual(self.store._rows('SELECT state FROM sticker_usage')[0]['state'], 'sending')

    def test_continue_exposes_only_safe_same_scene_pending_candidates(self):
        pending, _ = self.add(local=1)
        self.add(scene='bob', sender='bob', local=2)
        self.add(local=3, state='unconfirmed')
        job = self.job('刚才没收藏的继续')
        self.handle(job)
        self.assertEqual([item['id'] for item in job['sticker_continue_candidates']], [pending])
        self.assertEqual(self.library.get(pending)['native_state'], 'pending')

    def test_ordinary_text_and_unknown_tag_do_not_mutate_classification(self):
        identifier, _ = self.add()
        self.assertIsNone(self.handle(self.job('我觉得表情包挺好玩的')))
        reply = self.handle(self.job('把刚才的表情标成私人照片'))
        self.assertIn('标签', reply)
        self.assertEqual(json.loads(self.library.get(identifier)['tags']), ['生气'])

    def test_account_or_group_caller_mismatch_fails_closed(self):
        identifier, _ = self.add(scene='room@chatroom')
        job = self.job('这张是无奈，不是生气', 'room@chatroom')
        job['account'] = 'other'
        self.assertIn('无法确认', self.handle(job))
        job['account'] = 'account'
        job['focus']['message']['sender'] = 'bob'
        self.assertIn('无法确认', self.handle(job))
        self.assertEqual(json.loads(self.library.get(identifier)['tags']), ['生气'])


if __name__ == '__main__':
    unittest.main()
