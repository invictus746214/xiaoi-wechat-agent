import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from PIL import Image, ImageDraw
import sticker_media


def picture():
    image=Image.new('RGB',(80,80),'white')
    ImageDraw.Draw(image).rectangle((15,15,60,60),fill='orange')
    output=io.BytesIO()
    image.save(output,format='PNG')
    return output.getvalue()


class StickerMediaTests(unittest.TestCase):
    def test_dynamic_decoder_bounds_pixels_before_opening_input(self):
        with tempfile.TemporaryDirectory() as root, patch('sticker_media.ffmpeg_executable',return_value='/tool'), patch('sticker_media.subprocess.run',return_value=SimpleNamespace(returncode=1)) as run:
            self.assertEqual(sticker_media.frames(b'wxgf-test',root),[])
            args=run.call_args.args[0]
            self.assertLess(args.index('-max_pixels'),args.index('-i'))
            self.assertEqual(args[args.index('-max_pixels')+1],'2097152')
            self.assertEqual(run.call_args.kwargs['timeout'],12)
    def test_metadata_beyond_old_text_limit_is_preserved(self):
        digest='a'*32
        self.assertEqual(sticker_media.metadata('<msg><emoji padding="'+'x'*1200+'" md5="'+digest+'"/></msg>'),[digest])
        self.assertEqual(sticker_media.metadata('<emoji md5="../../other"/>'),[])

    @unittest.skipUnless(importlib.util.find_spec('Crypto'), 'server Crypto dependency')
    def test_message_bound_cache_decrypts_and_wrong_hash_is_rejected(self):
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            raw=picture()
            digest=hashlib.md5(raw).hexdigest()
            key=hashlib.md5(b'unit-test-account-EMOTICON').digest()
            key_file=root/'key.json'
            key_file.write_text(json.dumps({'account':key.hex()}))
            cache=root/'business'/'emoticon'/'Persist'/digest[:2]
            cache.mkdir(parents=True)
            (cache/digest).write_bytes(AES.new(key,AES.MODE_CBC,key).encrypt(pad(raw,16)))
            self.assertEqual(sticker_media.original_bytes(root,'account',[digest],key_file),raw)
            self.assertIsNone(sticker_media.original_bytes(root,'other',[digest],key_file))
            (cache/digest).write_bytes(b'wrong content')
            self.assertIsNone(sticker_media.original_bytes(root,'account',[digest],key_file))

    def test_original_image_samples_are_real_and_blank_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            paths=sticker_media.frames(picture(),root)
            self.assertEqual(len(paths),1)
            with Image.open(paths[0]) as image:
                self.assertEqual(image.size,(80,80))
            blank=io.BytesIO()
            Image.new('RGB',(40,40),'white').save(blank,format='PNG')
            self.assertEqual(sticker_media.frames(blank.getvalue(),root),[])
            self.assertTrue(sticker_media.meaningful(Image.new('RGB',(10,10),'red')))

    def test_symlink_outside_account_cannot_be_loaded(self):
        with tempfile.TemporaryDirectory() as root:
            account=Path(root)/'account'
            cache=account/'business'/'emoticon'/'Persist'
            cache.mkdir(parents=True)
            raw=picture(); digest=hashlib.md5(raw).hexdigest()
            outside=Path(root)/'outside.png'; outside.write_bytes(raw)
            try:
                (cache/digest).symlink_to(outside)
            except OSError:
                self.skipTest('symlink unavailable')
            self.assertIsNone(sticker_media.original_bytes(account,'account',[digest],Path(root)/'no-key'))


if __name__=='__main__':
    unittest.main()
