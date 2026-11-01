import io,hashlib,json,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from PIL import Image
import bridge,settings,database_service as service
from store import Store
from sticker_library import StickerLibrary,parse_reusable
from sticker_collect import CollectQueue

def original():
    output=io.BytesIO();image=Image.new('RGB',(40,40),'blue');image.putpixel((4,5),(255,255,255));image.save(output,format='PNG');return output.getvalue()

class StickerLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store();self.library=StickerLibrary(self.store,self.temp.name,'self')
        self.raw=original();self.digest=hashlib.md5(self.raw).hexdigest();self.stamp=time.time()
        self.message=dict(local_id=1,type=47,outgoing=False,sender='friend',timestamp=self.stamp,sticker_hashes=[self.digest])
        self.row=dict(source='s',event_id='incoming',message=self.message)
        self.job=dict(event_id='batch',account='self',username='friend',session_key='key',kind='private',message='[表情]',focus_text='[表情]',batch=[self.row],force_reply=False,voice_mode=False)
        self.cfg=settings.normalize({'stickers':{'enabled':True,'auto_send':True,'prepare_before_reply':True}})
    def tearDown(self):self.store.close();self.temp.cleanup()

    def test_semantic_tags_save_reusable_sticker_without_keyword_overlap(self):
        record=parse_reusable('{"reusable":true,"description":"一只章鱼端着咖啡，半睁着眼睛","tags":["无奈"]}')
        asset=self.library.collect(self.raw,[self.digest],record['description'],tags=record['tags'])
        self.assertTrue(asset);self.assertIn('无奈',json.loads(self.library.get(asset)['tags']))
        self.assertIsNone(parse_reusable('{"reusable":true,"description":"二维码截图","tags":["开心"]}'))

    def test_repeated_semantically_tagged_original_keeps_new_source_without_another_vision_call(self):
        asset=self.library.collect(self.raw,[self.digest],'一只章鱼端着咖啡',tags=['无奈'])
        reader=Mock(account='self');reader.db_dir=Path(self.temp.name)/'db_storage';vision=Mock()
        self.assertEqual(service.collect_sticker(self.library,reader,'friend',self.row,vision,raw=self.raw),asset)
        self.assertEqual(self.library.current_assets(self.job)[0]['id'],asset);vision.assert_not_called()

    def test_missing_original_is_not_mistaken_for_cached_original(self):
        reader=Mock();reader.db_dir=Path(self.temp.name)/'db_storage';reader.account='self';reader.messages.return_value=[self.message]
        ui=Mock();ui.latest_message.return_value=None
        with patch('sticker_media.original_bytes',return_value=None),patch('database_service.select_chat',return_value=True) as selected:
            service.capture_latest_media(ui,reader,self.job,bridge)
        selected.assert_called_once()

    def test_favorite_source_is_the_verified_sticker_not_following_text(self):
        from sticker_ui import favorite_source
        asset=dict(md5=self.digest)
        job=dict(self.job,batch=[self.row,dict(source='s',message=dict(local_id=2,type=1,text='看懂后回我一个合适的表情'))])
        self.assertIs(favorite_source(asset,job),self.row)
        self.assertTrue(service.sticker_requested(dict(self.job,focus_text='看懂后回我一个合适的表情')))
        self.assertTrue(service.sticker_requested(dict(self.job,focus_text='用刚才的表情回复我')))

    def test_favorite_scrolls_partial_top_sticker_fully_into_view_before_image_check(self):
        from sticker_ui import NativeStickers
        box=SimpleNamespace(x=0,y=0,width=500,height=418)
        clipped=SimpleNamespace(x=0,y=-23,width=500,height=152);full=SimpleNamespace(x=0,y=10,width=500,height=152)
        ui=Mock();ui.rows.side_effect=[[('Animated Stickers',clipped,None)],[('Animated Stickers',full,None)]];ui.incoming_state.return_value='incoming'
        reader=Mock();reader.messages.return_value=[self.message]
        b=Mock();b.component_rect.return_value=box;b.is_image_stub.return_value=True;b.image_capture_rect.return_value=(60,10,150,120);b.moments.walk.return_value=[]
        native=NativeStickers(ui,reader,b);native.favorite_records=Mock(return_value=[]);native.capture=Mock(return_value=None)
        result=native.favorite_latest(dict(md5=self.digest),self.job,lambda *args:True,lambda:True)
        self.assertEqual(result,'source_image_unverified')
        self.assertTrue(any(call.kwargs.get('up') for call in ui.pointer().scroll.call_args_list))

    def test_urgent_message_prevents_favorite_chat_selection(self):
        from sticker_ui import NativeStickers
        ui=Mock();reader=Mock();reader.messages.return_value=[self.message]
        native=NativeStickers(ui,reader,Mock(),should_yield=lambda:True)
        native.favorite_records=Mock(return_value=[]);select=Mock(return_value=False)
        result=native.favorite_latest(dict(md5=self.digest),self.job,select,lambda:True)
        self.assertEqual(result,'yield')
        select.assert_not_called()

    def test_urgent_message_after_decode_prevents_opening_context_menu(self):
        from sticker_ui import NativeStickers
        box=SimpleNamespace(x=0,y=0,width=500,height=418)
        row=SimpleNamespace(x=0,y=10,width=500,height=152)
        ui=Mock();ui.rows.return_value=[('Animated Stickers',row,None)];ui.incoming_state.return_value='incoming'
        reader=Mock();reader.messages.return_value=[self.message]
        b=Mock();b.component_rect.return_value=box;b.is_image_stub.return_value=True;b.image_capture_rect.return_value=(60,10,150,120)
        b.moments.walk.return_value=[]
        urgent=[False]
        native=NativeStickers(ui,reader,b,should_yield=lambda:urgent[0]);native.favorite_records=Mock(return_value=[])
        native.capture=Mock(return_value=Image.new('RGB',(150,120),'blue'))
        def decode(_):urgent[0]=True;return []
        native.templates=Mock(side_effect=decode)
        with patch('sticker_ui.match_tile',return_value=0),patch('sticker_ui.time.sleep'):
            self.assertEqual(native.favorite_latest(dict(md5=self.digest),self.job,lambda *args:True,lambda:True),'yield')
        ui.pointer().xtst.XTestFakeButtonEvent.assert_not_called()

    def test_submitted_favorite_yields_as_unconfirmed_without_another_click(self):
        from sticker_ui import NativeStickers
        box=SimpleNamespace(x=0,y=0,width=500,height=418)
        row=SimpleNamespace(x=0,y=10,width=500,height=152)
        ui=Mock();ui.rows.return_value=[('Animated Stickers',row,None)];ui.incoming_state.return_value='incoming'
        reader=Mock();reader.messages.return_value=[self.message]
        b=Mock();b.component_rect.return_value=box;b.is_image_stub.return_value=True;b.image_capture_rect.return_value=(60,10,150,120)
        control=Mock();control.name='Add Sticker';control.getRoleName.return_value='menu item'
        b.moments.walk.return_value=[control]
        urgent=[False]
        native=NativeStickers(ui,reader,b,should_yield=lambda:urgent[0]);native.favorite_records=Mock(return_value=[])
        native.capture=Mock(return_value=Image.new('RGB',(150,120),'blue'));native.templates=Mock(return_value=[])
        def clicked(*_):urgent[0]=True;return True
        b._click_rect.side_effect=clicked
        with patch('sticker_ui.match_tile',return_value=0),patch('sticker_ui.time.sleep'):
            self.assertEqual(native.favorite_latest(dict(md5=self.digest),self.job,lambda *args:True,lambda:True),'unconfirmed')
        b._click_rect.assert_called_once();native.favorite_records.assert_called_once()

    def test_new_media_during_decode_prevents_using_old_bubble_coordinates(self):
        from sticker_ui import NativeStickers
        box=SimpleNamespace(x=0,y=0,width=500,height=418);row=SimpleNamespace(x=0,y=10,width=500,height=152)
        ui=Mock();ui.rows.return_value=[('Animated Stickers',row,None)];ui.incoming_state.return_value='incoming'
        reader=Mock();reader.messages.side_effect=[[self.message],[self.message,dict(self.message,local_id=2,sticker_hashes=['c'*32])]]
        b=Mock();b.component_rect.return_value=box;b.is_image_stub.return_value=True;b.image_capture_rect.return_value=(60,10,150,120);b.moments.walk.return_value=[]
        native=NativeStickers(ui,reader,b);native.favorite_records=Mock(return_value=[])
        native.capture=Mock(return_value=Image.new('RGB',(150,120),'blue'));native.templates=Mock(return_value=[])
        with patch('sticker_ui.match_tile',return_value=0),patch('sticker_ui.time.sleep'):
            self.assertEqual(native.favorite_latest(dict(md5=self.digest),self.job,lambda *args:True,lambda:True),'source_not_latest')
        ui.pointer().xtst.XTestFakeButtonEvent.assert_not_called()

    def test_bubble_moved_during_decode_is_rechecked_before_click(self):
        from sticker_ui import NativeStickers
        box=SimpleNamespace(x=0,y=0,width=500,height=418);row=SimpleNamespace(x=0,y=10,width=500,height=152)
        ui=Mock();ui.rows.return_value=[('Animated Stickers',row,None)];ui.incoming_state.return_value='incoming'
        reader=Mock();reader.messages.return_value=[self.message]
        b=Mock();b.component_rect.return_value=box;b.is_image_stub.return_value=True;b.image_capture_rect.return_value=(60,10,150,120);b.moments.walk.return_value=[]
        original=Image.new('RGB',(150,120),'blue');moved=Image.new('RGB',(150,120),'red')
        native=NativeStickers(ui,reader,b);native.favorite_records=Mock(return_value=[])
        native.capture=Mock(side_effect=[original,moved]);native.templates=Mock(return_value=[original])
        with patch('sticker_ui.time.sleep'):
            self.assertEqual(native.favorite_latest(dict(md5=self.digest),self.job,lambda *args:True,lambda:True),'source_image_unverified')
        ui.pointer().xtst.XTestFakeButtonEvent.assert_not_called()

    def test_collection_restart_only_retries_readonly_collection_with_limit(self):
        queue=CollectQueue(self.store,'self');queue.enqueue('friend',self.row,now=self.stamp)
        job=queue.claim(set(),now=self.stamp)
        queue=CollectQueue(self.store,'self')
        self.assertIsNotNone(queue.claim(set(),now=self.stamp))
        queue.finish(job,'retry',now=self.stamp)
        for n in range(3):
            job=queue.claim(set(),now=self.stamp+1000*(n+1))
            if job:queue.finish(job,'retry',now=self.stamp+1000*(n+1))
        self.assertIsNone(queue.claim(set(),now=self.stamp+10000))

    def test_pending_favorite_includes_sticker_followed_by_text(self):
        asset=self.library.collect_message(Mock(account='self'),'friend',self.row,'小猫开心',raw=self.raw)
        job=dict(self.job,batch=[self.row,dict(source='s',event_id='text',message=dict(local_id=2,type=1,text='哈哈',timestamp=self.stamp,outgoing=False))])
        self.assertEqual(self.library.pending_for(job)[0]['id'],asset)

    def test_native_ready_phase_precedes_reply_and_does_not_repeat_vision(self):
        reader=Mock(account='self');reader.db_dir=Path(self.temp.name)/'db_storage'
        responder=Mock(return_value='接住这个梗了')
        vision=Mock(side_effect=['小猫开心大笑','{"reusable":true,"description":"小猫开心大笑","tags":["开心"]}'])
        with patch('sticker_media.original_bytes',return_value=self.raw):
            value=service.prepare_model(self.job,reader,responder,vision,bridge,self.cfg,stickers=self.library)
            self.assertEqual(value,service.STICKER_MEDIA_READY);responder.assert_not_called()
            self.job['favorites_attempted']=True
            self.assertEqual(service.prepare_model(self.job,reader,responder,vision,bridge,self.cfg,stickers=self.library),'接住这个梗了')
        self.assertEqual(vision.call_count,2)

    def test_failed_observation_is_not_reused_as_success(self):
        reader=Mock(account='self');reader.db_dir=Path(self.temp.name)/'db_storage'
        people=Mock();people.observation.return_value='未能读取，像是空白图片'
        cfg=settings.normalize({})
        with patch('sticker_media.original_bytes',return_value=self.raw):
            service.prepare_model(self.job,reader,lambda _:'看到了',lambda *args:'蓝色小猫开心大笑',bridge,cfg,people=people)
        people.observe.assert_called_once();self.assertIn('蓝色小猫',self.job['message'])

    def test_passive_collection_is_idempotent_bounded_and_skips_historical_outgoing(self):
        queue=CollectQueue(self.store,'self');now=self.stamp
        self.assertTrue(queue.enqueue('r@chatroom',self.row,now=now));self.assertFalse(queue.enqueue('r@chatroom',self.row,now=now))
        self.assertFalse(queue.enqueue('friend',dict(self.row,message=dict(self.message,local_id=2,outgoing=True)),now=now))
        self.assertFalse(queue.enqueue('friend',dict(self.row,message=dict(self.message,local_id=3,timestamp=now-3600)),now=now))
        self.assertIsNone(queue.claim({'r@chatroom'},now=now))
        job=queue.claim(set(),now=now);self.assertEqual(job['username'],'r@chatroom')
        queue.finish(job,'done',now=now);self.assertIsNone(queue.claim(set(),now=now+100))

    def test_capability_context_reports_supported_saved_and_confirmed_separately(self):
        asset=self.library.collect_message(Mock(account='self'),'friend',self.row,'小猫开心',raw=self.raw)
        facts=service.sticker_capabilities(self.job,self.library,self.cfg)
        self.assertTrue(facts['save_originals']);self.assertTrue(facts['send_native']);self.assertTrue(facts['current_original_saved'])
        self.assertFalse(facts['current_native_confirmed'])
        self.library.set_native(asset,'f'*32,'confirmed')
        self.assertTrue(service.sticker_capabilities(self.job,self.library,self.cfg)['current_native_confirmed'])

if __name__=='__main__':unittest.main()
