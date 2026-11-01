import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bridge
import settings
import wechat_db
from database_service import build_job, prepare_model, receipt, send_reply, recover_abandoned
from event_pipeline import Inbox, Reader
from store import Store


class DatabaseReaderTests(unittest.TestCase):
    def test_new_conversation_is_received_even_when_already_read(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = Inbox(str(Path(root) / "state.db"))
            sessions = []
            rows = []
            db = SimpleNamespace(account="account", keys={"message/message_0.db": {}}, sessions=lambda limit: sessions)
            db.messages = lambda user, limit, source, after_id=None: rows[-limit:] if after_id is None else [m for m in rows if m["local_id"] > after_id][:limit]
            worker = Reader(lambda: db, inbox, settings.normalize({}), lambda _: False)
            with patch("event_pipeline.time.time", return_value=100.9):
                worker.poll(db)
            sessions.append(dict(username="new-friend",title="new-friend", unread=0,last_timestamp=200))
            rows.append(dict(local_id=1,type=1,text="new",timestamp=100,outgoing=False))
            self.assertEqual(worker.poll(db), 1)
            self.assertEqual(inbox.count(), 1)
            inbox.close()

    def test_incremental_read_is_ascending_and_keeps_identical_text_events(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "messages.db")
            reader = wechat_db.WeChatDB.__new__(wechat_db.WeChatDB)
            reader.account = "self"
            reader._connect = lambda _: sqlite3.connect(path)
            table = "Msg_" + __import__("hashlib").md5(b"friend").hexdigest()
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE Name2Id(user_name TEXT)")
                db.execute("INSERT INTO Name2Id VALUES('friend')")
                db.execute(f"CREATE TABLE {table}(local_id INTEGER,local_type INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content TEXT,server_id INTEGER)")
                db.executemany(f"INSERT INTO {table} VALUES(?,1,1,100,'same',?)", [(n,n+1000) for n in range(1, 351)])
            db.close()
            first = reader.messages("friend", limit=200, after_id=0)
            second = reader.messages("friend", limit=200, after_id=first[-1]["local_id"])
            self.assertEqual([m["local_id"] for m in first + second], list(range(1,351)))
            self.assertEqual(reader.messages("friend", limit=2)[-1]["local_id"], 350)
            with self.assertRaises(ValueError):
                reader.messages("friend", source="../../secrets")

    def test_reader_bootstraps_unread_then_resumes_across_restart(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = Inbox(str(Path(root) / "state.db"))
            cfg = settings.normalize({})
            messages = [dict(local_id=n, type=1, text="hi", outgoing=False, timestamp=__import__("time").time()) for n in range(1, 5)]
            db = SimpleNamespace(account="account", keys={"message/message_0.db": {}},
                                 sessions=lambda limit: [dict(username="friend", title="friend", unread=2, last_timestamp=100)])
            db.messages = lambda user, limit, source, after_id=None: messages[-limit:] if after_id is None else [m for m in messages if m["local_id"]>after_id][:limit]
            worker = Reader(lambda: db, inbox, cfg, lambda _: False)
            self.assertEqual(worker.poll(db), 2)
            self.assertEqual(inbox.cursor("account", "friend", "message/message_0.db"), 4)
            messages.append(dict(messages[-1], local_id=5))
            restarted = Reader(lambda: db, inbox, cfg, lambda _: False)
            self.assertEqual(restarted.poll(db), 1)
            self.assertEqual(inbox.count(), 3)
            inbox.close()


class ReceiptTests(unittest.TestCase):
    def test_executor_exception_releases_chat_without_resubmitting_uncertain_send(self):
        inbox = Inbox(":memory:")
        store = Store()
        item = dict(local_id=1,type=1,text="hello",timestamp=100,outgoing=False)
        inbox.ingest("a","friend","message/message_0.db","friend",[item],4)
        batch = inbox.claim(set(), now=200)
        job = dict(batch=batch,event_id=batch[0]["batch_id"])
        recover_abandoned(inbox, store, job)
        with inbox.lock, inbox.db:
            inbox.db.execute("UPDATE inbox SET due=0")
        batch = inbox.claim(set(), now=300)
        self.assertIsNotNone(batch)
        job["batch"] = batch
        store.start_delivery("d","key",job["event_id"],"hello","reply")
        store.set_delivery_state("d","sending")
        recover_abandoned(inbox,store,job)
        self.assertEqual(inbox.count(),0)
        self.assertIsNone(inbox.claim(set(),now=1000))
        store.close()
        inbox.close()

    def test_receipt_requires_new_outgoing_correct_content_and_server_id(self):
        db = Mock()
        job = {"username": "friend"}
        db.messages.return_value = [dict(local_id=11, outgoing=True, type=1, text="reply", server_id="0")]
        self.assertFalse(receipt(db, job, 10, "reply"))
        db.messages.return_value[0]["server_id"] = "1234"
        self.assertTrue(receipt(db, job, 10, "reply"))
        self.assertFalse(receipt(db, job, 10, "different"))
        self.assertFalse(receipt(db, job, 10, "reply", voice=True))

    def test_uncertain_voice_is_not_followed_by_text_or_resubmitted(self):
        store = Store()
        ui = Mock()
        db = Mock()
        db.messages.return_value = []
        b = SimpleNamespace(delivery_key=bridge.delivery_key, voice_module=Mock(), same_chat=lambda *_: True)
        b.voice_module.send.return_value = True
        job = dict(username="friend", session_key="key", event_id="event", message="voice please", wav="audio", expected=(1,2,3))
        with patch("database_service.select_chat", return_value=True), patch("database_service.time.sleep"):
            self.assertEqual(send_reply(ui, db, job, "reply", store, b, {}), "unconfirmed")
            self.assertEqual(send_reply(ui, db, job, "reply", store, b, {}), "unconfirmed")
        b.voice_module.send.assert_called_once()
        ui.send.assert_not_called()
        store.close()

    def test_image_uses_its_own_message_md5_and_cached_reply_skips_models(self):
        row = dict(event_id="input", source="message/message_0.db", message=dict(type=3,image_md5="exact-image",text=""))
        job = dict(batch=[row], event_id="event", message="image", force_reply=False, voice_mode=False)
        db = Mock()
        db.decode_image.return_value = None
        responder = Mock(return_value="reply")
        cfg = settings.normalize({})
        self.assertEqual(prepare_model(job, db, responder, None, bridge, cfg), "reply")
        self.assertEqual(db.decode_image.call_args.args[0], "exact-image")
        row["reply"] = "saved"
        responder.reset_mock()
        self.assertEqual(prepare_model(job, db, responder, None, bridge, cfg), "saved")
        responder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
