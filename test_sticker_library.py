import hashlib
import io
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image
from store import Store
from sticker_library import StickerLibrary,parse_classification


def png():
    image=Image.new('RGB',(40,40),'blue')
    image.putpixel((2,3),(255,255,255))
    out=io.BytesIO();image.save(out,format='PNG');return out.getvalue()


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store()
        self.library=StickerLibrary(self.store,Path(self.temp.name)/'library','account')

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def add(self,description='小猫大笑，表达开心和好笑',kind=47):
        raw=png()
        return self.library.collect(raw,[hashlib.md5(raw).hexdigest()],description,kind=kind)

    def test_original_digest_dedupe_and_limited_private_permissions(self):
        first=self.add();second=self.add()
        self.assertEqual(first,second)
        self.assertEqual(len(self.library.items()),1)
        self.assertEqual(Path(self.library.get(first)['path']).read_bytes(),png())
        self.assertIsNone(self.library.collect(png(),['0'*32],'小猫开心'))
        self.assertEqual(self.library.get(first)['native_state'],'pending')

    def test_only_usable_stickers_are_collected_not_photos_private_or_meaningless(self):
        self.assertIsNone(self.add(kind=3))
        for desc in ('聊天截图含有手机号码','一张身份证照片','二维码加好友','无法看清','一张风景照片'):
            self.assertIsNone(self.add(desc))
        self.assertIsNotNone(self.add())

    def test_account_boundary_and_no_unverified_native_send(self):
        item=self.add()
        self.assertIsNone(self.library.choose('哈哈笑死我了','太好笑了','scene','event',explicit=True))
        self.library.set_native(item,'f'*32,'confirmed')
        self.assertEqual(self.library.choose('哈哈笑死我了','太好笑了','scene','event',explicit=True)['id'],item)
        other=StickerLibrary(self.store,Path(self.temp.name)/'other','other-account')
        self.assertIsNone(other.get(item))
        self.assertEqual(other.items(),[])

    def test_serious_context_cooldown_repeat_and_unknown_never_retry(self):
        item=self.add();self.library.set_native(item,'f'*32,'confirmed')
        self.assertIsNone(self.library.choose('医院手术需要多少钱','我帮你查查','s','e',explicit=True))
        self.library.record_send(item,'s','event','unconfirmed')
        self.assertIsNone(self.library.choose('哈哈','太好笑了','s','e2',explicit=True))
        self.assertIsNotNone(self.library.choose('哈哈','太好笑了','other','e3',explicit=True))

    def test_asset_limit_is_not_unbounded(self):
        limited=StickerLibrary(self.store,Path(self.temp.name)/'limited','account',max_bytes=10)
        self.assertIsNone(limited.collect(png(),[hashlib.md5(png()).hexdigest()],'开心大笑'))

    def test_collection_uses_explicit_boolean_not_mentions_of_privacy(self):
        self.assertEqual(parse_classification('{"reusable":true,"description":"卡通小猫开心大笑"}'),'卡通小猫开心大笑')
        self.assertEqual(parse_classification('{"reusable":false,"description":"聊天截图"}'),'')
        self.assertEqual(parse_classification('{"reusable":"true","description":"卡通小猫"}'),'')
        self.assertEqual(parse_classification('无隐私，小猫开心'),'')

    def test_unknown_sticker_requires_a_fresh_explicit_repeat_request(self):
        identifier=self.add();self.library.set_native(identifier,'f'*32,'confirmed')
        self.library.record_send(identifier,'s','previous','confirmed')
        self.assertIsNotNone(self.library.choose('再发个开心的表情','好','s','new-request',explicit=True))
        self.library.record_send(identifier,'s','previous','unconfirmed')
        self.assertIsNone(self.library.choose('发个开心的表情','好','s','new-request',explicit=True))
        self.assertIsNone(self.library.choose('再发个开心的表情','好','s','previous',explicit=True))
        self.assertIsNotNone(self.library.choose('再发个开心的表情','好','s','new-request',explicit=True))
        self.assertIsNone(self.library.choose('再发个开心的表情','好','s','automatic',explicit=False))

    def test_generic_explicit_request_can_choose_an_available_safe_sticker(self):
        identifier=self.add();self.library.set_native(identifier,'f'*32,'confirmed')
        self.assertIsNotNone(self.library.choose('发个表情包','给你','s','generic',explicit=True))

    def test_declining_stickers_overrides_explicit_and_automatic_selection(self):
        identifier=self.add();self.library.set_native(identifier,'f'*32,'confirmed')
        for text in ('不要再发表情包','别再发开心表情包','不用重新发','不要\n再发表情包'):
            for explicit in (True,False):
                self.assertIsNone(self.library.choose(text,'开心','s','event',explicit=explicit))


if __name__=='__main__':unittest.main()
