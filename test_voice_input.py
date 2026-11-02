import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import voice_input


class VoiceInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.message = dict(account='self', chat_username='friend', sender='friend',
                            local_id=7, server_id='1234', timestamp=10,
                            type=34, raw_type=34, outgoing=False,
                            source='message/message_0.db')
        self.raw = b'\x02#!SILK_V3' + b'\x03\x00abc'
        table = 'Msg_' + hashlib.md5(b'friend').hexdigest()
        with closing(sqlite3.connect(self.root/'message.db')) as db, db:
            db.execute('CREATE TABLE Name2Id(user_name TEXT)')
            db.execute("INSERT INTO Name2Id VALUES('friend')")
            db.execute(f'CREATE TABLE {table}(local_id,server_id,local_type,create_time,real_sender_id)')
            db.execute(f'INSERT INTO {table} VALUES(7,1234,34,10,1)')
        with closing(sqlite3.connect(self.root/'media.db')) as db, db:
            db.execute('CREATE TABLE Name2Id(user_name TEXT)')
            db.execute("INSERT INTO Name2Id VALUES('friend')")
            db.execute('CREATE TABLE VoiceInfo(chat_name_id,local_id,svr_id,create_time,voice_data)')
            db.execute('INSERT INTO VoiceInfo VALUES(1,7,1234,10,?)', (self.raw,))
        self.calls = []
        def connect(source):
            self.calls.append(source)
            name = 'message.db' if source == 'message/message_0.db' else 'media.db'
            return sqlite3.connect((self.root/name).as_uri()+'?mode=ro', uri=True)
        self.reader = SimpleNamespace(account='self', db_dir=self.root,
                                      keys={'message/media_0.db': {}}, _connect=connect)
        self.cfg = {'voice_input': {'enabled': True}}

    def test_disabled_never_opens_database_or_worker(self):
        self.assertEqual(voice_input.transcribe_message(self.reader, self.message, {}), '')
        self.assertEqual(self.calls, [])

    def test_only_exact_incoming_account_message_can_be_transcribed(self):
        for changes in ({'account': 'other'}, {'type': 1}, {'outgoing': True},
                        {'source': '../other.db'}, {'server_id': '0'},
                        {'server_id': '-1'}, {'sender': 'other'}, {'timestamp': 11},
                        {'chat_username': 'other'}, {'local_id': 8}):
            with self.subTest(changes=changes), patch('voice_input.available', return_value=True), \
                    patch('voice_input.subprocess.run') as run:
                self.assertEqual(voice_input.transcribe_message(self.reader, {**self.message, **changes}, self.cfg), '')
                run.assert_not_called()

    def test_verified_silk_is_passed_only_to_local_bounded_worker(self):
        result = SimpleNamespace(returncode=0, stdout=json.dumps({'text': '你好'}).encode())
        with patch('voice_input.available', return_value=True), \
                patch('voice_input.subprocess.run', return_value=result) as run:
            self.assertEqual(voice_input.transcribe_message(self.reader, self.message, self.cfg), '你好')
        self.assertEqual(run.call_args.kwargs['input'], self.raw)
        self.assertEqual(run.call_args.kwargs['timeout'], 15)
        self.assertNotIn('shell', run.call_args.kwargs)
        self.assertIn('-I', run.call_args.args[0])

    def test_duplicate_or_oversize_or_unrecognized_blob_is_rejected(self):
        for payload, duplicate in ((self.raw, True), (b'x'*2_000_001, False), (b'RIFFbad', False)):
            with self.subTest(duplicate=duplicate, size=len(payload)):
                with closing(sqlite3.connect(self.root/'media.db')) as db, db:
                    db.execute('DELETE FROM VoiceInfo')
                    db.execute('INSERT INTO VoiceInfo VALUES(1,7,1234,10,?)', (payload,))
                    if duplicate:
                        db.execute('INSERT INTO VoiceInfo VALUES(1,7,1234,10,?)', (payload,))
                with patch('voice_input.available', return_value=True), patch('voice_input.subprocess.run') as run:
                    self.assertEqual(voice_input.transcribe_message(self.reader, self.message, self.cfg), '')
                    run.assert_not_called()

    def test_worker_error_or_timeout_never_invents_text(self):
        for result in (SimpleNamespace(returncode=1, stdout=b''),
                       SimpleNamespace(returncode=0, stdout=b'not json'),
                       SimpleNamespace(returncode=0, stdout=b'{"text":42}')):
            with patch('voice_input.available', return_value=True), patch('voice_input.subprocess.run', return_value=result):
                self.assertEqual(voice_input.transcribe_message(self.reader, self.message, self.cfg), '')
        with patch('voice_input.available', return_value=True), \
                patch('voice_input.subprocess.run', side_effect=subprocess.TimeoutExpired('worker', 15)):
            self.assertEqual(voice_input.transcribe_message(self.reader, self.message, self.cfg), '')

    def test_timeout_cleans_parent_owned_scratch_and_worker_uses_one_thread(self):
        directories = []
        def timeout(args, **kwargs):
            scratch = Path(args[-1])
            directories.append(scratch)
            (scratch/'partial.pcm').write_bytes(b'private audio')
            self.assertEqual(kwargs['env']['OMP_NUM_THREADS'], '1')
            self.assertEqual(kwargs['env']['OPENBLAS_NUM_THREADS'], '1')
            raise subprocess.TimeoutExpired(args, 15)
        with patch('voice_input.available', return_value=True), patch('voice_input.subprocess.run', side_effect=timeout):
            self.assertEqual(voice_input.transcribe_message(self.reader, self.message, self.cfg), '')
        self.assertEqual(len(directories), 1)
        self.assertFalse(directories[0].exists())

    def test_busy_worker_skips_second_model(self):
        voice_input._LOCK.acquire()
        try:
            with patch('voice_input.subprocess.run') as run:
                self.assertEqual(voice_input.transcribe_message(self.reader, self.message, self.cfg), '')
                run.assert_not_called()
        finally:
            voice_input._LOCK.release()

    def test_decoded_duration_is_checked_before_asr_model_load(self):
        def decode(source, target, **kwargs):
            Path(target).write_bytes(b'\0'*(16000*2+2))
        vosk = SimpleNamespace(SetLogLevel=lambda level: None, Model=unittest.mock.Mock())
        with patch.dict('sys.modules', {'pilk': SimpleNamespace(decode=decode), 'vosk': vosk}):
            self.assertEqual(voice_input._recognize(self.raw, self.root, 1, self.root), '')
        vosk.Model.assert_not_called()

    def test_local_asr_text_is_actual_recognizer_result(self):
        def decode(source, target, **kwargs):
            self.assertEqual(kwargs['pcm_rate'], 16000)
            Path(target).write_bytes(b'\0'*8000)
        recognizer = SimpleNamespace(AcceptWaveform=lambda data: True,
                                     Result=lambda: '{"text":"你好"}',
                                     FinalResult=lambda: '{"text":"朋友"}')
        vosk = SimpleNamespace(SetLogLevel=lambda level: None, Model=lambda path: object(),
                               KaldiRecognizer=lambda model, rate: recognizer)
        with patch.dict('sys.modules', {'pilk': SimpleNamespace(decode=decode), 'vosk': vosk}):
            self.assertEqual(voice_input._recognize(self.raw, self.root, 60, self.root), '你好 朋友')

    def test_silk_packet_validation_rejects_truncation_and_too_many_frames(self):
        self.assertTrue(voice_input._valid_silk(self.raw, 60))
        self.assertFalse(voice_input._valid_silk(self.raw[:-1], 60))
        self.assertFalse(voice_input._valid_silk(b'\x02#!SILK_V3' + b'\x01\x00a'*3001, 60))

    def test_missing_model_stays_unavailable(self):
        self.assertFalse(voice_input.available({'voice_input': {'enabled': True, 'model_path': str(self.root/'missing')}}))

    def test_resident_memory_guard_stops_oversized_worker(self):
        stop=unittest.mock.Mock();stop.wait.side_effect=[False,True]
        with patch('voice_input.Path.read_text',return_value='VmRSS: 500000 kB\n'),patch('voice_input.os._exit',side_effect=SystemExit(2)) as exited:
            with self.assertRaises(SystemExit):voice_input._memory_watchdog(stop)
        exited.assert_called_once_with(2)

    def test_resident_memory_guard_allows_measured_small_model(self):
        stop=unittest.mock.Mock();stop.wait.side_effect=[False,True]
        with patch('voice_input.Path.read_text',return_value='VmRSS: 250000 kB\n'),patch('voice_input.os._exit') as exited:
            voice_input._memory_watchdog(stop)
        exited.assert_not_called()


if __name__ == '__main__':
    unittest.main()
