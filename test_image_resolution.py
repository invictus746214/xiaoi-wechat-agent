import io
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

import bridge
from database_service import capture_latest_media, prepare_model
import settings
import wechat_db


class ImageResolutionTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.root=Path(self.folder.name)
        self.reader=wechat_db.WeChatDB.__new__(wechat_db.WeChatDB)
        self.reader.db_dir=self.root/'account'/'db_storage'
        self.reader.cache_dir=self.root/'cache'
        self.img=self.reader.db_dir.parent/'msg'/'attach'/'chat-hash'/'2026-10'/'Img'
        self.img.mkdir(parents=True)
        self.content_hash='a'*32
        self.cache_hash='b'*32
        self.resource=self.root/'resource.db'
        with sqlite3.connect(self.resource) as db:
            db.execute('CREATE TABLE MessageResourceInfo(message_svr_id INTEGER,packed_info BLOB)')
            db.execute('INSERT INTO MessageResourceInfo VALUES(1234,?)',(b'\x0a\x22\x0a\x20'+self.cache_hash.encode(),))
            db.execute('INSERT INTO MessageResourceInfo VALUES(9999,?)',(b'\x0a\x22\x0a\x20'+('c'*32).encode(),))
        db.close()
        self.reader._connect=lambda _:sqlite3.connect(self.resource)

    def tearDown(self): self.folder.cleanup()

    def test_resource_server_id_resolves_original_and_keeps_messages_separate(self):
        original=self.img/(self.cache_hash+'.dat'); original.write_bytes(b'cached')
        (self.img/(self.cache_hash+'_t.dat')).write_bytes(b'thumb')
        self.assertEqual(Path(self.reader.image_path(self.content_hash,server_id='1234')),original)
        self.assertIsNone(self.reader.image_path(self.content_hash,server_id='9999'))
        self.assertIsNone(self.reader.image_path(self.content_hash,server_id='0'))
        self.assertIsNone(self.reader.image_path(self.content_hash))

    def test_thumbnail_is_exact_bound_fallback_and_path_traversal_is_rejected(self):
        thumb=self.img/(self.cache_hash+'_t.dat');thumb.write_bytes(b'thumb')
        self.assertEqual(Path(self.reader.image_path(self.content_hash,server_id='1234')),thumb)
        self.assertIsNone(self.reader.image_path('../*',server_id='1234'))
        self.assertIsNone(self.reader.image_path(self.content_hash,server_id="1234 OR 1=1"))

    def test_old_exact_digest_lookup_works_without_resource_database(self):
        original=self.img/(self.content_hash+'.dat'); original.write_bytes(b'cached')
        self.reader._connect=Mock(side_effect=FileNotFoundError())
        self.assertEqual(Path(self.reader.image_path(self.content_hash)),original)
        self.reader._connect.assert_not_called()

    @unittest.skipIf(wechat_db._AES is None,'AES unavailable')
    def test_bound_resource_file_is_decrypted_as_actual_image(self):
        from PIL import Image
        source=io.BytesIO()
        Image.new('RGB',(24,30),'red').save(source,format='JPEG')
        data=source.getvalue(); key=b'0123456789abcdef'; xor=91
        padded=data[:128]+bytes([16])*16
        encrypted=wechat_db._AES.new(key,wechat_db._AES.MODE_ECB).encrypt(padded)
        blob=b'\x07\x08V2\x08\x07'+struct.pack('<LL',128,64)+b'\x00'+encrypted+data[128:-64]+bytes(v^xor for v in data[-64:])
        (self.img/(self.cache_hash+'.dat')).write_bytes(blob)
        self.reader.image_key=lambda:(key.decode(),xor)
        decoded=self.reader.decode_image(self.content_hash,out_dir=self.root/'decoded',server_id='1234')
        with Image.open(decoded) as image: self.assertEqual(image.size,(24,30))


class PhotoPipelineTests(unittest.TestCase):
    def setUp(self):
        self.message=dict(local_id=8,type=3,image_md5='a'*32,server_id='1234',outgoing=False,text='')
        self.row=dict(event_id='photo',source='message/message_0.db',message=self.message)
        self.job=dict(batch=[self.row],username='friend',kind='private',event_id='batch',force_reply=False,voice_mode=False)
        self.db=Mock();self.db.messages.return_value=[self.message]

    def test_model_worker_passes_exact_server_id_to_decoder(self):
        self.db.decode_image.return_value=None
        prepare_model(self.job,self.db,lambda job:'audit',None,bridge,settings.normalize({}))
        self.assertEqual(self.db.decode_image.call_args.kwargs.get('server_id'),'1234')

    def test_missing_native_cache_with_md5_still_captures_verified_photo(self):
        self.db.image_path.return_value=None
        ui=Mock(); ui.latest_message.return_value=('Image',object(),None)
        ui.incoming_state.return_value='incoming'
        def capture(rect,path):
            from PIL import Image,ImageDraw
            image=Image.new('RGB',(30,30),'white')
            ImageDraw.Draw(image).rectangle((4,4,20,20),fill='red')
            image.save(path); return True
        ui.capture_region.side_effect=capture
        with patch('database_service.select_chat',return_value=True),patch('database_service.time.sleep'),patch.object(bridge,'image_capture_rect',return_value=object()):
            capture_latest_media(ui,self.db,self.job,bridge)
        self.assertEqual(len(self.job.get('captures',[])),1)
        self.assertEqual(self.job['capture_event'],'photo')
        for name in self.job['captures']: Path(name).unlink()

    def test_existing_bound_native_cache_skips_ui_capture(self):
        self.db.image_path.return_value='exact-photo.dat'
        ui=Mock()
        capture_latest_media(ui,self.db,self.job,bridge)
        self.db.image_path.assert_called_once_with(self.message['image_md5'],server_id='1234')
        ui.latest_message.assert_not_called()


if __name__=='__main__': unittest.main()
