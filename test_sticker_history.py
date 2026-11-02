import hashlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from sticker_library import StickerLibrary
from sticker_ui import NativeStickers
from store import Store


class StickerHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=Store(str(Path(self.temp.name)/'state.db'))
        self.addCleanup(lambda:self.store.close())
        self.library=StickerLibrary(self.store,self.temp.name,'self')
        self.image=Image.new('RGB',(80,80),'blue')
        self.image.putpixel((12,19),(255,255,255))
        out=io.BytesIO();self.image.save(out,format='PNG');raw=out.getvalue()
        self.md5=hashlib.md5(raw).hexdigest()
        self.source=dict(local_id=1,type=47,outgoing=False,sticker_hashes=[self.md5],timestamp=int(time.time()))
        self.row=dict(source='message/message_0.db',message=self.source)
        identifier=self.library.collect_message(SimpleNamespace(account='self'),'friend',self.row,'小猫开心',raw=raw)
        self.asset=self.library.get(identifier)
        self.job=self.library.favorite_jobs()[0]
        self.newest=dict(self.source,local_id=3,sticker_hashes=['c'*32])

    def gui(self,target=2,duplicates=False):
        state=SimpleNamespace(offset=0,urgent=False)
        bounds=SimpleNamespace(x=0,y=0,width=500,height=420)
        box=SimpleNamespace(x=0,y=10,width=500,height=150)
        second=SimpleNamespace(x=0,y=180,width=500,height=150)
        def scroll(count=1,up=False):
            state.offset=max(0,min(8,state.offset+(count if up else -count)))
        pointer=SimpleNamespace(scroll=Mock(side_effect=scroll),move=Mock(),tap=Mock(),
            xtst=SimpleNamespace(XTestFakeButtonEvent=Mock()),x11=SimpleNamespace(XFlush=Mock()),display=None)
        ui=SimpleNamespace(right_list=SimpleNamespace(parent=object()),app=object(),pyatspi=SimpleNamespace(STATE_SHOWING=1),composer=None,
            pointer=lambda:pointer,selected_info=lambda:('Friend','/friend'))
        ui.rows=lambda _: [('Animated Sticker',box,None)]+([('Animated Sticker',second,None)] if duplicates else [])
        ui.incoming_state=Mock(return_value='incoming')
        control=SimpleNamespace(name='Add Sticker',getRoleName=lambda:'menu item',getState=lambda:SimpleNamespace(contains=lambda _:True))
        bar=SimpleNamespace(x=490,y=0,width=10,height=420,getRoleName=lambda:'scroll bar',
            getState=lambda:SimpleNamespace(contains=lambda _:True),
            queryValue=lambda:SimpleNamespace(currentValue=8-state.offset,maximumValue=8,minimumValue=0))
        reader=SimpleNamespace(account='self',messages=Mock(return_value=[self.source,self.newest]))
        b=SimpleNamespace(component_rect=lambda obj,_:bounds if obj is ui.right_list else obj,
            is_image_stub=lambda name:name.startswith('Animated'),image_capture_rect=lambda _ui,r,_group:(60,r.y,80,80),
            moments=SimpleNamespace(walk=lambda root,*_:[control] if root is ui.app else [bar]),wechat_session=lambda:(42,),_click_rect=Mock(return_value=True))
        native=NativeStickers(ui,reader,b,should_yield=lambda:state.urgent)
        records=[[]]
        native.favorite_records=lambda:records[0]
        native.capture=Mock(side_effect=lambda _:self.image if state.offset==target else Image.new('RGB',(80,80),'red'))
        native.templates=Mock(return_value=[self.image])
        def submit():
            records[0]=[(self.md5,[self.md5])]
            return True
        def select(*_):
            self.job.update(title='Friend',path='/friend')
            return True
        return native,state,pointer,Mock(side_effect=submit),select

    def test_navigation_progress_survives_reopen_and_is_account_scoped(self):
        self.job.update(history_anchor='a'*64,history_offset=3)
        self.library.save_favorite_progress(self.asset['id'],self.job)
        self.store.close();self.store=Store(str(Path(self.temp.name)/'state.db'))
        self.library=StickerLibrary(self.store,self.temp.name,'self')
        saved=self.library.favorite_jobs()[0]
        self.assertEqual((saved['history_anchor'],saved['history_offset']),('a'*64,3))
        other=StickerLibrary(self.store,self.temp.name,'other')
        self.assertEqual(other.favorite_jobs(),[])
        changed=dict(self.job,username='another')
        changed['history_offset']=9
        self.library.save_favorite_progress(self.asset['id'],changed)
        self.assertEqual(self.library.favorite_jobs()[0]['history_offset'],3)

    def test_old_source_is_located_and_submitted_once(self):
        native,state,pointer,submit,select=self.gui()
        with patch('sticker_ui.time.sleep'),patch('sticker_ui.focus_owned',return_value=True):
            result=native.favorite_source(self.asset,self.job,select,submit)
        self.assertEqual(result,self.md5)
        self.assertEqual(state.offset,2)
        self.assertEqual(submit.call_count,1)
        self.assertEqual(native.b._click_rect.call_count,1)

    def test_duplicate_original_bubbles_fail_closed(self):
        native,state,pointer,submit,select=self.gui(target=0,duplicates=True)
        with patch('sticker_ui.time.sleep'),patch('sticker_ui.focus_owned',return_value=True):
            result=native.favorite_source(self.asset,self.job,select,submit)
        self.assertEqual(result,'source_image_unverified')
        self.assertEqual(submit.call_count,0)
        self.assertEqual(pointer.xtst.XTestFakeButtonEvent.call_count,0)

    def test_forged_source_direction_account_or_hash_never_moves_ui(self):
        for change in ({'outgoing':True},{'type':3},{'sticker_hashes':['b'*32]},{'local_id':99}):
            native,state,pointer,submit,select=self.gui()
            native.reader.messages.return_value=[dict(self.source,**change),self.newest]
            self.assertEqual(native.favorite_source(self.asset,self.job,select,submit),'source_unverified')
            self.assertEqual(pointer.scroll.call_count,0)
        native,state,pointer,submit,select=self.gui();native.reader.account='other'
        self.assertEqual(native.favorite_source(self.asset,self.job,select,submit),'account_unverified')
        self.assertEqual(pointer.scroll.call_count,0)

    def test_urgent_yield_saves_progress_and_resume_reseeks_bottom(self):
        native,state,pointer,submit,select=self.gui(target=3)
        original=pointer.scroll.side_effect
        def urgent_scroll(count=1,up=False):
            original(count,up)
            if up and state.offset==2:state.urgent=True
        pointer.scroll.side_effect=urgent_scroll
        with patch('sticker_ui.time.sleep'),patch('sticker_ui.focus_owned',return_value=True):
            self.assertEqual(native.favorite_source(self.asset,self.job,select,submit),'yield')
        self.assertEqual(self.job['history_offset'],2)
        self.library.save_favorite_progress(self.asset['id'],self.job)
        self.job=self.library.favorite_jobs()[0]
        native,state,pointer,submit,select=self.gui(target=3)
        state.offset=6
        with patch('sticker_ui.time.sleep'),patch('sticker_ui.focus_owned',return_value=True):
            self.assertEqual(native.favorite_source(self.asset,self.job,select,submit),self.md5)
        self.assertFalse(pointer.scroll.call_args_list[0].kwargs.get('up',False))
        self.assertEqual(state.offset,3)

    def test_new_latest_anchor_resets_stale_history_offset(self):
        native,state,pointer,submit,select=self.gui(target=0)
        self.job.update(history_offset=7,history_anchor='old')
        with patch('sticker_ui.time.sleep'),patch('sticker_ui.focus_owned',return_value=True):
            self.assertEqual(native.favorite_source(self.asset,self.job,select,submit),self.md5)
        self.assertEqual(state.offset,0)
        self.assertNotEqual(self.job['history_anchor'],'old')

    def test_urgent_after_submit_is_unconfirmed_and_never_clicks_again(self):
        native,state,pointer,submit,select=self.gui(target=0)
        native.b._click_rect.side_effect=lambda *_:setattr(state,'urgent',True) or True
        with patch('sticker_ui.time.sleep'),patch('sticker_ui.focus_owned',return_value=True):
            self.assertEqual(native.favorite_source(self.asset,self.job,select,submit),'unconfirmed')
        self.assertEqual(submit.call_count,1)
        self.assertEqual(native.b._click_rect.call_count,1)

    def test_missing_scrollbar_never_assumes_bottom_from_identical_rows(self):
        native,state,pointer,submit,select=self.gui(target=0)
        native.b.moments.walk=lambda *_:[]
        with patch('sticker_ui.time.sleep'),patch('sticker_ui.focus_owned',return_value=True):
            self.assertEqual(native.favorite_source(self.asset,self.job,select,submit),'history_bottom_unverified')
        self.assertEqual(submit.call_count,0)
        self.assertEqual(pointer.scroll.call_count,0)

    def test_empty_selection_fails_closed_before_wheel_or_click(self):
        native,state,pointer,submit,select=self.gui()
        native.ui.selected_info=lambda:('','')
        with patch('sticker_ui.focus_owned',return_value=True):
            self.assertEqual(native.favorite_source(self.asset,self.job,select,submit),'chat_unverified')
        self.assertEqual(pointer.scroll.call_count,0)
        self.assertEqual(submit.call_count,0)

    def test_budget_expiry_yields_without_submission(self):
        native,state,pointer,submit,select=self.gui(target=7)
        ticks=iter([0,0,1,2,3,4,5]+[6]*100)
        with patch('sticker_ui.time.monotonic',side_effect=lambda:next(ticks)),patch('sticker_ui.time.sleep'),patch('sticker_ui.focus_owned',return_value=True):
            self.assertEqual(native.favorite_source(self.asset,self.job,select,submit),'history_pending')
        self.assertEqual(submit.call_count,0)

    def test_same_local_id_but_changed_latest_metadata_invalidates_offset(self):
        native,state,pointer,submit,select=self.gui(target=0)
        native.reader.messages.side_effect=[[self.source,self.newest],[dict(self.newest,server_id='changed')]]
        with patch('sticker_ui.time.sleep'),patch('sticker_ui.focus_owned',return_value=True):
            self.assertEqual(native.favorite_source(self.asset,self.job,select,submit),'history_pending')
        self.assertEqual(self.job['history_offset'],0)
        self.assertEqual(submit.call_count,0)

    def test_source_latest_uses_existing_verified_fast_path_without_scrollbar(self):
        native,state,pointer,submit,select=self.gui(target=0)
        native.reader.messages.return_value=[self.source]
        native.favorite_latest=Mock(return_value=self.md5)
        self.assertEqual(native.favorite_source(self.asset,self.job,select,submit),self.md5)
        self.assertEqual(pointer.scroll.call_count,0)

    def test_repeated_native_source_is_ambiguous_before_history_navigation(self):
        native,state,pointer,submit,select=self.gui()
        native.reader.messages.return_value=[self.source,dict(self.source,local_id=2),self.newest]
        self.assertEqual(native.favorite_source(self.asset,self.job,select,submit),'source_unverified')
        self.assertEqual(pointer.scroll.call_count,0)

    def test_false_submit_guard_dismisses_without_favoriting(self):
        native,state,pointer,submit,select=self.gui(target=0)
        submit=Mock(return_value=False)
        with patch('sticker_ui.time.sleep'),patch('sticker_ui.focus_owned',return_value=True):
            self.assertEqual(native.favorite_source(self.asset,self.job,select,submit),'yield')
        self.assertEqual(native.b._click_rect.call_count,0)
        self.assertEqual(submit.call_count,1)

    def test_unique_latest_native_text_verifies_bottom_when_scrollbar_is_hidden(self):
        native,state,pointer,submit,select=self.gui(target=2)
        latest=dict(local_id=4,type=1,outgoing=False,text='unique latest native anchor',timestamp=99)
        native.reader.messages.return_value=[self.source,self.newest,latest]
        original_rows=native.ui.rows
        def rows(root):
            result=original_rows(root)
            if state.offset==0:result.append((latest['text'],SimpleNamespace(x=0,y=260,width=500,height=50),None))
            return result
        native.ui.rows=rows
        original_walk=native.b.moments.walk
        native.b.moments.walk=lambda root,*args:original_walk(root,*args) if root is native.ui.app else []
        state.offset=3
        with patch('sticker_ui.time.sleep'),patch('sticker_ui.focus_owned',return_value=True):
            self.assertEqual(native.favorite_source(self.asset,self.job,select,submit),self.md5)
        self.assertEqual(state.offset,2)
        self.assertEqual(submit.call_count,1)

    def test_clipped_later_row_cannot_prove_bottom_from_an_older_text_anchor(self):
        native,state,pointer,submit,select=self.gui(target=0)
        latest=dict(local_id=4,type=1,outgoing=False,text='unique latest native anchor')
        rows=[(latest['text'],SimpleNamespace(x=0,y=260,width=500,height=50),None),
            ('Animated Sticker',SimpleNamespace(x=0,y=350,width=500,height=150),None)]
        native.ui.rows=lambda _:rows
        self.assertFalse(native._history_text_bottom([self.source,latest],SimpleNamespace(x=0,y=0,width=500,height=420)))


if __name__=='__main__':unittest.main()
