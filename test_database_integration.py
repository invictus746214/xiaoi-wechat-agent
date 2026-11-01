"""Synthetic end-to-end run: no network or real GUI."""
import signal
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bridge
import database_service
import settings


class IntegrationTests(unittest.TestCase):
    def test_receive_generate_send_confirm_and_restart_without_repeat(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "state.db")
            cfg = settings.normalize({"memory":{"db":path}, "metrics":{"enabled":False},
                                      "moments":{"enabled":False}, "voice":{"enabled":False},
                                      "delivery":{"quota_enabled":False}})
            messages = [dict(local_id=1,type=1,text="hello",timestamp=time.time(),outgoing=False,sender="friend",server_id="1")]
            db = SimpleNamespace(account="account",keys={"message/message_0.db":{}},
                                 sessions=lambda limit: [dict(username="friend",title="friend",unread=1,last_timestamp=100)],
                                 contacts=lambda: {"friend":"friend"})
            db.messages = lambda user, limit=30, after_id=None, source=None: messages[-limit:] if after_id is None else [m for m in messages if m["local_id"]>after_id][:limit]
            handlers = {}
            ui = Mock()
            ui.rows.return_value = []

            def send(*args,**kwargs):
                messages.append(dict(local_id=2,type=1,text="reply",timestamp=time.time(),outgoing=True,sender="account",server_id="456"))
                handlers[signal.SIGTERM](None,None)
                return "sent"

            def select(_ui,_db,job,_b):
                job["expected"] = ("title","path","latest")
                return True

            ui.send.side_effect = send
            responder = Mock(return_value="reply")
            with patch.object(bridge.wechat_db_module,"WeChatDB",return_value=db), \
                 patch.object(bridge,"wechat_session",return_value=("123","bus",":1","auth")), \
                 patch.object(bridge,"bind_session"), patch.object(bridge,"WeChatUI",return_value=ui), \
                 patch.object(bridge.settings,"read_api_key",return_value="test"), \
                 patch.object(bridge,"build_responder",return_value=responder), \
                 patch.object(database_service,"active_account",return_value=True), \
                 patch.object(database_service,"migrate_memory"), \
                 patch.object(database_service,"select_chat",side_effect=select), \
                 patch.object(database_service.signal,"signal",side_effect=lambda sig,fn:handlers.update({sig:fn})):
                self.assertEqual(database_service.run(cfg),0)
            with sqlite3.connect(path) as result:
                self.assertEqual(result.execute("SELECT state FROM inbox").fetchone()[0],"done")
                self.assertEqual(result.execute("SELECT state FROM outbox").fetchone()[0],"confirmed")
            result.close()
            responder.assert_called_once()
            ui.send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
