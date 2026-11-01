import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bridge
import settings
from database_service import send_reply, capture_latest_media, prepare_model
from store import Store


class BubbleTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.cfg = settings.normalize({"delivery":{"quota_enabled":False,"split_threshold":10,"split_target":10,"split_max":3}})
        self.job = dict(username="friend",session_key="key",event_id="batch",message="user message",kind="private",expected=(1,2,3))
        self.reply = "第一句内容比较长。\n第二句内容也比较长。\n第三句是最后一个意思。"
        self.db = Mock()
        self.db.messages.return_value = []
        self.ui = Mock()

    def tearDown(self):
        self.store.close()

    def test_retry_preserves_parts_and_skips_confirmed_part(self):
        self.ui.send.side_effect = ["sent","retry","sent","sent"]
        with patch("database_service.select_chat",return_value=True), patch("database_service.receipt",return_value=True), patch("database_service.time.sleep"):
            self.assertEqual(send_reply(self.ui,self.db,self.job,self.reply,self.store,bridge,self.cfg),"retry")
            self.cfg["delivery"]["split_enabled"] = False
            self.assertEqual(send_reply(self.ui,self.db,self.job,self.reply,self.store,bridge,self.cfg),"done")
        calls = [c.args[0] for c in self.ui.send.call_args_list]
        self.assertEqual(calls,["第一句内容比较长。","第二句内容也比较长。","第二句内容也比较长。","第三句是最后一个意思。"])
        self.assertEqual(len(self.store.recent("key",20)),4)
        self.assertEqual(self.store.delivery_count("key",0),3)

    def test_restart_keeps_text_transport_even_if_tts_recovers(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root,"state.db")
            state = Store(path)
            self.ui.send.side_effect = ["sent","retry","sent","sent"]
            try:
                with patch("database_service.select_chat",return_value=True), patch("database_service.receipt",return_value=True), patch("database_service.time.sleep"):
                    self.assertEqual(send_reply(self.ui,self.db,self.job,self.reply,state,bridge,self.cfg),"retry")
                    self.assertIn("第一句",str(state.recent("key",20)))
                    state.close()
                    state = Store(path)
                    state.recover_deliveries()
                    self.job["wav"] = "tts-now-works.wav"
                    self.assertEqual(send_reply(self.ui,self.db,self.job,"different spoken truncation",state,bridge,self.cfg),"done")
                self.assertEqual([c.args[0] for c in self.ui.send.call_args_list].count("第一句内容比较长。"),1)
                self.assertEqual(state.delivery_count("key",0),3)
            finally:
                state.close()

    def test_unknown_second_part_stops_remaining_parts_and_future_retries(self):
        self.ui.send.side_effect = ["sent","uncertain"]
        outcomes = [True] + [False] * 16
        with patch("database_service.select_chat",return_value=True), patch("database_service.receipt",side_effect=outcomes), patch("database_service.time.sleep"):
            self.assertEqual(send_reply(self.ui,self.db,self.job,self.reply,self.store,bridge,self.cfg),"unconfirmed")
            self.assertEqual(send_reply(self.ui,self.db,self.job,self.reply,self.store,bridge,self.cfg),"unconfirmed")
        self.assertEqual(self.ui.send.call_count,2)
        self.assertNotIn("第三句",str(self.store.recent("key",20)))

    def test_short_reply_is_not_padded_or_split(self):
        self.ui.send.return_value = "sent"
        with patch("database_service.select_chat",return_value=True), patch("database_service.receipt",return_value=True):
            self.assertEqual(send_reply(self.ui,self.db,self.job,"好呀",self.store,bridge,self.cfg),"done")
        self.ui.send.assert_called_once()

    def test_each_bubble_obeys_enabled_quota(self):
        self.cfg["delivery"].update(quota_enabled=True,max_per_minute=1,private_min_interval_seconds=0)
        self.ui.send.return_value="sent"
        with patch("database_service.select_chat",return_value=True), patch("database_service.receipt",return_value=True), patch("database_service.time.sleep"):
            self.assertEqual(send_reply(self.ui,self.db,self.job,self.reply,self.store,bridge,self.cfg),"defer")
        self.ui.send.assert_called_once()
        self.assertGreater(self.job["defer_seconds"],0)

    def test_new_incoming_message_cancels_unsent_parts(self):
        self.job["batch"] = [dict(source="message/message_0.db", message=dict(local_id=1))]
        self.ui.send.return_value = "sent"
        self.db.messages.side_effect = [[], [dict(local_id=3,outgoing=False)]]
        with patch("database_service.select_chat",return_value=True), patch("database_service.receipt",return_value=True), patch("database_service.time.sleep"):
            self.assertEqual(send_reply(self.ui,self.db,self.job,self.reply,self.store,bridge,self.cfg),"done")
        self.ui.send.assert_called_once()
        self.assertNotIn("第三句", str(self.store.recent("key",20)))


class StickerTests(unittest.TestCase):
    def test_three_frames_and_chat_context_reach_vision(self):
        db = Mock()
        db.messages.return_value = [dict(local_id=8)]
        row = dict(event_id="sticker",source="message/message_0.db",message=dict(local_id=8,type=47,text="",sender="friend"))
        job = dict(batch=[row],username="friend",kind="private",event_id="batch",force_reply=False,voice_mode=False,visual_context="对方刚说今天终于下班了")
        ui = Mock()
        ui.latest_message.return_value = ("Animated Sticker", object(), None)
        ui.incoming_state.return_value = "incoming"

        def capture(rect,path):
            from PIL import Image
            image=Image.new('RGB',(20,20),'white')
            image.putpixel((5,5),(0,0,0))
            image.save(path)
            return True

        ui.capture_region.side_effect = capture
        vision = Mock(return_value="人物跳起来，表达开心")
        with patch("database_service.select_chat",return_value=True), patch("database_service.time.sleep"), patch.object(bridge,"image_capture_rect",return_value=object()):
            capture_latest_media(ui,db,job,bridge)
        paths = list(job["captures"])
        self.assertEqual(len(paths),3)
        prepare_model(job,db,lambda _:"可算熬到下班了",vision,bridge,settings.normalize({}))
        self.assertEqual(len(vision.call_args.args[0]),3)
        self.assertIn("终于下班",vision.call_args.args[1])
        self.assertTrue(all(not os.path.exists(p) for p in paths))


if __name__ == "__main__":
    unittest.main()
