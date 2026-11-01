import unittest
from PIL import Image,ImageDraw

from sticker_ui import match_tile, sticker_receipt,NativeStickers
from sticker_ui import focus_owned,popup_windows,WindowAttributes
from unittest.mock import Mock
from unittest.mock import patch
import ctypes as ct
from types import SimpleNamespace
import bridge


class StickerUITests(unittest.TestCase):
    def test_rectangular_bubble_preserves_artwork_aspect_ratio(self):
        for size in ((160,60),(60,160)):
            image=Image.new('RGB',size,'white')
            draw=ImageDraw.Draw(image)
            draw.rectangle((5,5,size[0]//2,size[1]-5),fill='navy')
            draw.ellipse((size[0]//2,5,size[0]-5,size[1]-5),fill='red')
            other=Image.new('RGB',size,'green')
            self.assertEqual(match_tile([image],[other,image]),1)
            self.assertIsNone(match_tile([image],[image,image]))

    def test_exact_image_matches_and_ambiguous_tiles_are_rejected(self):
        image=Image.new('RGB',(72,72),'blue')
        image.putpixel((30,30),(255,255,255))
        other=Image.new('RGB',(72,72),'red')
        self.assertEqual(match_tile([image],[other,image]),1)
        self.assertIsNone(match_tile([image],[image,image]))
        self.assertIsNone(match_tile([image],[other]))

    def test_native_receipt_requires_new_outgoing_47_exact_hash_and_server_id(self):
        row=dict(local_id=6,outgoing=True,type=47,sticker_hashes=['a'*32],server_id='123')
        self.assertTrue(sticker_receipt([row],5,'a'*32))
        for change in (dict(type=3),dict(outgoing=False),dict(local_id=5),dict(sticker_hashes=['b'*32]),dict(server_id='0')):
            self.assertFalse(sticker_receipt([dict(row,**change)],5,'a'*32))

    def test_native_popup_focus_must_belong_to_the_wechat_process(self):
        api=SimpleNamespace(XInternAtom=Mock(return_value=1),XFree=Mock(),XQueryTree=Mock())
        api.XGetInputFocus=Mock(side_effect=lambda display,window,revert:setattr(window._obj,'value',50))
        pid=ct.c_ulong(123)
        def prop(*args):
            args[8]._obj.value=32;args[9]._obj.value=1
            ct.cast(args[-1],ct.POINTER(ct.POINTER(ct.c_ubyte)))[0]=ct.cast(ct.pointer(pid),ct.POINTER(ct.c_ubyte))
            return 0
        api.XGetWindowProperty=Mock(side_effect=prop)
        pointer=SimpleNamespace(x11=api,display=None)
        self.assertTrue(focus_owned(pointer,123))
        self.assertFalse(focus_owned(pointer,999))

    def test_new_urgent_message_prevents_any_sticker_ui_action(self):
        native=NativeStickers(None,None,None,should_yield=lambda:True)
        self.assertEqual(native.send({},lambda:True,lambda:True),'yield')

    def test_submitted_popup_stays_open_until_caller_checks_receipt(self):
        ui=SimpleNamespace(composer=Mock(),pointer=Mock())
        ui.composer.queryText.return_value.characterCount=0
        behavior=SimpleNamespace(wechat_session=lambda:(123,),_click_rect=Mock(return_value=True))
        native=NativeStickers(ui,None,behavior)
        native.favorites=Mock(return_value=['a'*32]);native.open=Mock(return_value=object())
        native.templates=Mock(return_value=[]);native.tiles=Mock(return_value=[object()])
        native.capture=Mock(return_value=Image.new('RGB',(72,72),'blue'));native.close=Mock()
        with patch('sticker_ui.match_tile',return_value=0),patch('sticker_ui.focus_owned',return_value=True),patch('sticker_ui.time.sleep'):
            self.assertEqual(native.send({'native_md5':'a'*32},lambda:True,lambda:True),'submitted')
        native.close.assert_not_called()

    def test_empty_chat_list_is_not_a_native_sticker_popup(self):
        ancestor=SimpleNamespace(rect=SimpleNamespace(x=0,y=0,width=876,height=640))
        ancestor.parent=ancestor
        chat=SimpleNamespace(path='empty-chat',rect=SimpleNamespace(x=0,y=0,width=574,height=418),parent=ancestor)
        popup=SimpleNamespace(rect=SimpleNamespace(x=0,y=0,width=466,height=466),parent=ancestor)
        grid=SimpleNamespace(path='sticker-grid',rect=SimpleNamespace(x=0,y=12,width=466,height=405),parent=popup)
        for obj in (chat,grid):
            obj.getRoleName=lambda:'list';obj.getState=lambda:SimpleNamespace(contains=lambda _:True)
        ui=SimpleNamespace(app=Mock(),pyatspi=SimpleNamespace(STATE_SHOWING=1),left_list=None,right_list=None)
        behavior=SimpleNamespace(component_rect=lambda obj,_:obj.rect,moments=SimpleNamespace(walk=lambda *_:[chat,grid]))
        other=SimpleNamespace(path='other-window-list',rect=SimpleNamespace(x=0,y=0,width=438,height=510),parent=SimpleNamespace(rect=SimpleNamespace(x=0,y=0,width=440,height=560),parent=ancestor))
        other.getRoleName=lambda:'list';other.getState=lambda:SimpleNamespace(contains=lambda _:True)
        behavior.moments.walk=lambda *_:[chat,other,grid]
        native=NativeStickers(ui,None,behavior)
        native.button=Mock(side_effect=lambda label,obj:object() if obj is grid else None)
        self.assertIs(native.panel(),grid)

    def test_existing_native_popup_is_reused_even_when_main_window_is_active(self):
        panel=Mock();ui=SimpleNamespace(window_active=lambda:True)
        native=NativeStickers(ui,None,SimpleNamespace(_click_rect=Mock(return_value=True)))
        native.panel=Mock(return_value=panel);native.button=Mock(return_value=Mock())
        native.activate_panel=Mock(return_value=True)
        with patch('sticker_ui.time.sleep'):
            self.assertIs(native.open(),panel)

    def test_empty_accessible_panel_does_not_recursively_rediscover_itself(self):
        class EmptyPanel(SimpleNamespace):
            def __bool__(self):return False
        panel=EmptyPanel(parent=None)
        control=SimpleNamespace(name='Custom Stickers',getRoleName=lambda:'page tab',getState=lambda:SimpleNamespace(contains=lambda _:True))
        ui=SimpleNamespace(pyatspi=SimpleNamespace(STATE_SHOWING=1))
        rect=SimpleNamespace(width=10,height=10)
        behavior=SimpleNamespace(moments=SimpleNamespace(walk=lambda *_:[control]),component_rect=lambda *_:rect)
        native=NativeStickers(ui,None,behavior);native.panel=Mock(side_effect=AssertionError('recursive discovery'))
        self.assertIs(native.button('Custom Stickers',panel),rect)

    def test_managed_popup_client_uses_root_coordinates(self):
        buffers=[]
        api=SimpleNamespace(XDefaultRootWindow=Mock(return_value=1),XFree=Mock())
        def query(display,w,root,parent,ids,count):
            found={1:[2],2:[3]}.get(w,[])
            array=(ct.c_ulong*len(found))(*found);buffers.append(array)
            ct.cast(ids,ct.POINTER(ct.POINTER(ct.c_ulong)))[0]=ct.cast(array,ct.POINTER(ct.c_ulong))
            count._obj.value=len(found);return 1
        def attrs(display,w,out):
            a=out._obj;a.map_state=2;a.x=0;a.y=0;a.width=466;a.height=466;return 1
        def translate(display,w,root,ox,oy,rx,ry,child):
            rx._obj.value=298;ry._obj.value=205;return 1
        api.XQueryTree=Mock(side_effect=query);api.XGetWindowAttributes=Mock(side_effect=attrs)
        api.XTranslateCoordinates=Mock(side_effect=translate)
        self.assertIn((3,(298,205,466,466)),popup_windows(SimpleNamespace(x11=api,display=None)))

    def test_green_picture_content_does_not_turn_incoming_sticker_into_outgoing(self):
        for left,want in ((80,'incoming'),(400,'outgoing')):
            image=Image.new('RGB',(600,200),(250,250,250))
            image.paste((40,210,80),(left,40,left+120,160))
            pixbuf=SimpleNamespace(get_pixels=lambda:image.tobytes(),get_rowstride=lambda:1800,get_n_channels=lambda:3)
            ui=bridge.WeChatUI.__new__(bridge.WeChatUI)
            ui.window_active=lambda:True;ui.screen_size=(600,200);ui.screen=None
            ui.Gdk=SimpleNamespace(pixbuf_get_from_window=lambda *_:pixbuf)
            ui.position_direction=lambda _:'unknown'
            rect=SimpleNamespace(x=0,y=20,width=600,height=160)
            self.assertEqual(ui.incoming_state('Animated Sticker',rect),want)

    def test_only_unique_mapped_popup_of_same_process_is_activated(self):
        ancestor=SimpleNamespace(rect=SimpleNamespace(x=10,y=8,width=466,height=466))
        ancestor.parent=ancestor
        panel=SimpleNamespace(rect=SimpleNamespace(x=10,y=20,width=466,height=405),parent=ancestor)
        api=SimpleNamespace(XRaiseWindow=Mock(),XSetInputFocus=Mock(),XSync=Mock())
        pointer=SimpleNamespace(x11=api,display=None)
        ui=SimpleNamespace(pyatspi=None,pointer=lambda:pointer)
        behavior=SimpleNamespace(component_rect=lambda obj,_:obj.rect,wechat_session=lambda:(123,))
        native=NativeStickers(ui,None,behavior)
        with patch('sticker_ui.popup_windows',return_value=[(77,(10,8,466,466))]),patch('sticker_ui.focus_owned',return_value=True):
            self.assertTrue(native.activate_panel(panel))
        api.XSetInputFocus.assert_called_once_with(None,77,2,0)
        api.XSetInputFocus.reset_mock()
        with patch('sticker_ui.popup_windows',return_value=[(77,(10,8,466,466))]),patch('sticker_ui.focus_owned',return_value=False):
            self.assertFalse(native.activate_panel(panel))
        api.XSetInputFocus.assert_not_called()


if __name__=='__main__':unittest.main()
