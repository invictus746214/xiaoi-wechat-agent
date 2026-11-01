import io
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from PIL import Image

import bridge
import settings
from database_service import sync_sticker,send_sticker,prepare_model,sticker_should_yield,sticker_requested
from event_pipeline import Inbox
from sticker_library import StickerLibrary
from store import Store


class StickerServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store()
        self.library=StickerLibrary(self.store,self.temp.name,'account')
        data=io.BytesIO();im=Image.new('RGB',(40,40),'blue');im.putpixel((4,5),(255,255,255));im.save(data,format='PNG')
        self.raw=data.getvalue();self.md5=hashlib.md5(self.raw).hexdigest()
        self.asset=self.library.collect(self.raw,[self.md5],'小猫大笑开心')
        self.message=dict(local_id=1,type=47,outgoing=False,sender='friend',timestamp=__import__('time').time(),sticker_hashes=[self.md5])
        self.row=dict(event_id='input',source='message/message_0.db',message=self.message)
        self.job=dict(event_id='event',account='account',username='friend',session_key='key',kind='private',message='发个开心表情包',batch=[self.row],force_reply=False,voice_mode=False)
        self.library.collect_message(Mock(account='account'), 'friend',self.row,'小猫大笑开心',raw=self.raw)
        self.cfg=settings.normalize({'delivery':{'quota_enabled':False},'stickers':{'auto_send':True}})

    def tearDown(self):self.store.close();self.temp.cleanup()

    def test_sync_confirms_native_id_and_does_not_repeat_unknown(self):
        native=Mock();native.favorite_records.return_value=[]
        def add(asset,job,select,before):before();return 'f'*32
        native.favorite_latest.side_effect=add
        reader=Mock();reader.messages.return_value=[]
        with patch('sticker_ui.NativeStickers',return_value=native):
            self.assertEqual(sync_sticker(Mock(),reader,self.job,self.library,self.store,bridge),'confirmed')
        self.assertEqual(self.library.get(self.asset)['native_state'],'confirmed')
        self.assertEqual(self.library.get(self.asset)['native_md5'],'f'*32)

    def test_send_requires_native_receipt_and_replay_never_clicks_again(self):
        self.library.set_native(self.asset,'f'*32,'confirmed')
        reader=Mock()
        reader.keys={'message/message_0.db':{}}
        reader.messages.side_effect=[[],[],[dict(local_id=2,outgoing=True,type=47,server_id='12',sticker_hashes=['f'*32])]]
        native=Mock()
        native.send.side_effect=lambda asset,guard,submit:'submitted' if submit() else 'yield'
        with patch('sticker_ui.NativeStickers',return_value=native),patch('database_service.select_chat',return_value=True):
            result=send_sticker(Mock(),reader,self.job,'来啦',self.library,self.store,bridge,self.cfg,explicit=True)
            self.assertEqual(result,'confirmed')
            self.assertEqual(send_sticker(Mock(),reader,self.job,'来啦',self.library,self.store,bridge,self.cfg,explicit=True),'confirmed')
        native.send.assert_called_once()
        native.close.assert_called_once()

    def test_optional_sticker_does_not_join_unmentioned_group_interjection(self):
        self.library.set_native(self.asset,'f'*32,'confirmed')
        job=dict(self.job,kind='group',group_reply_mode='interject')
        self.assertEqual(send_sticker(Mock(),Mock(),job,'哈哈',self.library,self.store,bridge,self.cfg),'skipped')

    def test_vision_collection_is_connected_to_actual_worker(self):
        self.library.store._write('DELETE FROM sticker_assets')
        reader=Mock(account='account')
        reader.db_dir=Path(self.temp.name)/'db_storage'
        with patch('sticker_media.original_bytes',return_value=self.raw):
            prepare_model(self.job,reader,lambda _: '哈哈很好笑',lambda paths,prompt:'{"reusable":true,"description":"小猫大笑开心"}' if 'reusable' in prompt else '小猫大笑开心',bridge,self.cfg,stickers=self.library)
        self.assertEqual(len(self.library.items()),1)
        self.assertEqual(len(self.library.pending_for(self.job)),1)

    def test_library_description_excludes_private_conversation_context(self):
        self.library.store._write('DELETE FROM sticker_assets')
        reader=Mock(account='account');reader.db_dir=Path(self.temp.name)/'db_storage'
        vision=Mock(side_effect=['小猫大笑，可能是在回应私聊里的秘密称呼','{"reusable":true,"description":"小猫大笑，表达开心"}'])
        with patch('sticker_media.original_bytes',return_value=self.raw):
            prepare_model(self.job,reader,lambda _: '哈哈',vision,bridge,self.cfg,stickers=self.library)
        self.assertNotIn('秘密称呼',self.library.items()[0]['description'])
        self.assertNotIn('聊天前文',vision.call_args.args[1])

    def test_new_private_message_preempts_group_sticker_work(self):
        inbox=Inbox(':memory:')
        try:
            inbox.ingest('account','room@chatroom','s','group',[dict(self.message,timestamp=100,type=1,text='@小i')],3)
            batch=inbox.claim(set(),now=200)
            job=dict(self.job,username='room@chatroom',batch=batch)
            self.assertFalse(sticker_should_yield(inbox,job))
            inbox.ingest('other','friend','s','other',[dict(self.message,timestamp=101,type=1,text='hello')],4)
            self.assertFalse(sticker_should_yield(inbox,job))
            inbox.ingest('account','friend','s','friend',[dict(self.message,timestamp=101,type=1,text='hello')],4)
            self.assertTrue(sticker_should_yield(inbox,job))
        finally:inbox.close()

    def test_disabled_sticker_send_keeps_text_pipeline_and_never_touches_gui(self):
        self.cfg['stickers']['auto_send']=False
        with patch('sticker_ui.NativeStickers') as native:
            self.assertEqual(send_sticker(Mock(),Mock(),self.job,'好的',self.library,self.store,bridge,self.cfg),'skipped')
        native.assert_not_called()

    def test_declining_stickers_never_opens_native_gui(self):
        for text in ('不要再发表情包','别再发开心表情包','不用重新发'):
            job=dict(self.job,message=text)
            self.assertFalse(sticker_requested(job))
            with patch('sticker_ui.NativeStickers') as native:
                self.assertEqual(send_sticker(Mock(),Mock(),job,'开心',self.library,self.store,bridge,self.cfg,explicit=True),'skipped')
            native.assert_not_called()


if __name__=='__main__':unittest.main()
