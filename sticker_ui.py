"""Native sticker UI actions, guarded by original-image and database checks."""
import hashlib
import ctypes as ct
import io
from pathlib import Path
import tempfile
import time

from PIL import Image,ImageChops,ImageOps,ImageStat
import sticker_media


def focus_owned(pointer,pid,window_id=None):
    x=pointer.x11;display=pointer.display
    x.XGetInputFocus.argtypes=[ct.c_void_p,ct.POINTER(ct.c_ulong),ct.POINTER(ct.c_int)]
    x.XInternAtom.argtypes=[ct.c_void_p,ct.c_char_p,ct.c_int];x.XInternAtom.restype=ct.c_ulong
    x.XGetWindowProperty.argtypes=[ct.c_void_p,ct.c_ulong,ct.c_ulong,ct.c_long,ct.c_long,ct.c_int,ct.c_ulong,ct.POINTER(ct.c_ulong),ct.POINTER(ct.c_int),ct.POINTER(ct.c_ulong),ct.POINTER(ct.c_ulong),ct.POINTER(ct.POINTER(ct.c_ubyte))]
    x.XQueryTree.argtypes=[ct.c_void_p,ct.c_ulong,ct.POINTER(ct.c_ulong),ct.POINTER(ct.c_ulong),ct.POINTER(ct.POINTER(ct.c_ulong)),ct.POINTER(ct.c_uint)]
    x.XFree.argtypes=[ct.c_void_p]
    window=ct.c_ulong(window_id or 0);revert=ct.c_int()
    if window_id is None:x.XGetInputFocus(display,ct.byref(window),ct.byref(revert))
    atom=x.XInternAtom(display,b'_NET_WM_PID',False)
    for _ in range(10):
        if window.value in (0,1):return False
        actual=ct.c_ulong();fmt=ct.c_int();count=ct.c_ulong();remaining=ct.c_ulong();data=ct.POINTER(ct.c_ubyte)()
        status=x.XGetWindowProperty(display,window,atom,0,1,False,0,ct.byref(actual),ct.byref(fmt),ct.byref(count),ct.byref(remaining),ct.byref(data))
        try:
            if status==0 and fmt.value==32 and count.value==1 and data:
                return ct.cast(data,ct.POINTER(ct.c_ulong))[0]==int(pid)
        finally:
            if data:x.XFree(data)
        root=ct.c_ulong();parent=ct.c_ulong();children=ct.POINTER(ct.c_ulong)();n=ct.c_uint()
        ok=x.XQueryTree(display,window,ct.byref(root),ct.byref(parent),ct.byref(children),ct.byref(n))
        if children:x.XFree(children)
        if not ok or parent.value==window.value:return False
        window=parent
    return False


class WindowAttributes(ct.Structure):
    _fields_=[(name,kind) for name,kind in (
        ('x',ct.c_int),('y',ct.c_int),('width',ct.c_int),('height',ct.c_int),('border_width',ct.c_int),('depth',ct.c_int),
        ('visual',ct.c_void_p),('root',ct.c_ulong),('window_class',ct.c_int),('bit_gravity',ct.c_int),('win_gravity',ct.c_int),
        ('backing_store',ct.c_int),('backing_planes',ct.c_ulong),('backing_pixel',ct.c_ulong),('save_under',ct.c_int),
        ('colormap',ct.c_ulong),('map_installed',ct.c_int),('map_state',ct.c_int),('all_event_masks',ct.c_long),
        ('your_event_mask',ct.c_long),('do_not_propagate_mask',ct.c_long),('override_redirect',ct.c_int),('screen',ct.c_void_p))]


def popup_windows(pointer):
    x=pointer.x11;display=pointer.display
    x.XDefaultRootWindow.argtypes=[ct.c_void_p];x.XDefaultRootWindow.restype=ct.c_ulong
    x.XQueryTree.argtypes=[ct.c_void_p,ct.c_ulong,ct.POINTER(ct.c_ulong),ct.POINTER(ct.c_ulong),ct.POINTER(ct.POINTER(ct.c_ulong)),ct.POINTER(ct.c_uint)]
    x.XGetWindowAttributes.argtypes=[ct.c_void_p,ct.c_ulong,ct.POINTER(WindowAttributes)]
    x.XTranslateCoordinates.argtypes=[ct.c_void_p,ct.c_ulong,ct.c_ulong,ct.c_int,ct.c_int,ct.POINTER(ct.c_int),ct.POINTER(ct.c_int),ct.POINTER(ct.c_ulong)]
    x.XFree.argtypes=[ct.c_void_p]
    root_window=x.XDefaultRootWindow(display);found=[]
    def visit(window,depth):
        root=ct.c_ulong();parent=ct.c_ulong();children=ct.POINTER(ct.c_ulong)();count=ct.c_uint()
        if not x.XQueryTree(display,window,ct.byref(root),ct.byref(parent),ct.byref(children),ct.byref(count)):return
        try:ids=[int(children[i]) for i in range(min(count.value,1000 if depth==0 else 100))]
        finally:
            if children:x.XFree(children)
        for wid in ids:
            attr=WindowAttributes()
            if x.XGetWindowAttributes(display,wid,ct.byref(attr)) and attr.map_state==2:
                rx=ct.c_int();ry=ct.c_int();child=ct.c_ulong()
                if x.XTranslateCoordinates(display,wid,root_window,0,0,ct.byref(rx),ct.byref(ry),ct.byref(child)):
                    found.append((wid,(rx.value,ry.value,attr.width,attr.height)))
                if depth==0:visit(wid,1)
    visit(root_window,0)
    return found


def match_tile(templates,tiles,threshold=.08,margin=.025):
    scores=[]
    for tile in tiles:
        target=tile.convert('RGB').resize((32,32))
        corners=[target.getpixel(p) for p in ((0,0),(31,0),(0,31),(31,31))]
        background=tuple(sorted(c[i] for c in corners)[2] for i in range(3))
        target=ImageOps.pad(tile.convert('RGB'),(32,32),color=background)
        best=1.0
        for template in templates:
            for size in (32,29,26,23):
                rgba=template.convert('RGBA')
                rgba=ImageOps.contain(rgba,(size,size))
                canvas=Image.new('RGBA',(32,32),(*background,255))
                canvas.alpha_composite(rgba,((32-rgba.width)//2,(32-rgba.height)//2))
                delta=ImageStat.Stat(ImageChops.difference(canvas.convert('RGB'),target)).mean
                best=min(best,sum(delta)/(3*255))
        scores.append(best)
    ranked=sorted(enumerate(scores),key=lambda pair:pair[1])
    if not ranked or ranked[0][1]>threshold or (len(ranked)>1 and ranked[1][1]-ranked[0][1]<margin):
        return None
    return ranked[0][0]


def sticker_receipt(rows,before,digest):
    return any(row['local_id']>before and row.get('outgoing') and (row.get('type',0)&65535)==47
               and row.get('server_id') not in ('',None,'0',0) and digest in row.get('sticker_hashes',[]) for row in rows)

def favorite_source(asset,job):
    return next((row for row in reversed(job.get('batch',[])) if (row['message'].get('type',0)&65535)==47 and asset['md5'] in (row['message'].get('sticker_hashes') or sticker_media.metadata(row['message'].get('text','')))),None)


class NativeStickers:
    def __init__(self,ui,reader,b,should_yield=None):
        self.ui=ui;self.reader=reader;self.b=b
        self.should_yield=should_yield or (lambda:False)
        self._panel=None
        self._toggle=None

    def favorites(self):
        db=self.reader._connect('emoticon/emoticon.db')
        try:
            return [str(row[0]).lower() for row in db.execute('SELECT md5 FROM kFavEmoticonOrderTable ORDER BY rowid')]
        finally:db.close()

    def favorite_records(self):
        db=self.reader._connect('emoticon/emoticon.db')
        try:
            return [(str(row[0]).lower(),[str(h).lower() for h in row if h]) for row in db.execute('SELECT f.md5,n.extern_md5 FROM kFavEmoticonOrderTable f LEFT JOIN kNonStoreEmoticonTable n ON f.md5=n.md5 ORDER BY f.rowid')]
        finally:db.close()

    def panel(self):
        self.ui.app.clear_cache()
        result=[]
        excluded={str(obj.path) for obj in (self.ui.left_list,self.ui.right_list) if obj is not None}
        for index,obj in enumerate(self.b.moments.walk(self.ui.app,5000)):
            if index%100==0 and self.should_yield():return None
            try:
                if obj.getRoleName()!='list' or str(obj.path) in excluded or not obj.getState().contains(self.ui.pyatspi.STATE_SHOWING):continue
                rect=self.b.component_rect(obj,self.ui.pyatspi)
                if 100<rect.width<700 and 100<rect.height<600 and self.panel_bounds(obj) and self.button('Custom Stickers',obj) is not None:result.append(obj)
            except Exception:continue
        return result[0] if len(result)==1 else None

    def button(self,label,panel=None):
        root=panel if panel is not None else self.panel()
        for _ in range(4):
            if root is None:break
            for index,obj in enumerate(self.b.moments.walk(root,3000)):
                if index%100==0 and self.should_yield():return None
                try:
                    if obj.getRoleName() in ('push button','toggle button','page tab') and (obj.name or '').strip()==label and obj.getState().contains(self.ui.pyatspi.STATE_SHOWING):
                        rect=self.b.component_rect(obj,self.ui.pyatspi)
                        if rect.width>0 and rect.height>0:return rect
                except Exception:continue
            root=root.parent
        return None

    def open(self):
        if self.should_yield():return None
        panel=self.panel()
        if panel is None:
            toggle=self.b._find_push_rect(self.ui,'Send Sticker(Alt+E)',root=self.ui.frame)
            if self.should_yield() or toggle is None or not self.b._click_rect(self.ui,toggle):return None
            self._toggle=toggle
            time.sleep(.5)
            panel=self.panel()
        if panel is None:return None
        self._panel=panel
        custom=self.button('Custom Stickers',panel)
        if self.should_yield() or custom is None or not self.b._click_rect(self.ui,custom):return None
        time.sleep(.4)
        panel.clear_cache()
        self._panel=panel
        if not self.activate_panel(panel):return None
        return panel

    def panel_bounds(self,panel):
        grid=self.b.component_rect(panel,self.ui.pyatspi)
        expected=set();ancestor=panel
        for _ in range(5):
            if ancestor is None:break
            rect=self.b.component_rect(ancestor,self.ui.pyatspi)
            if abs(rect.width-grid.width)<=2 and grid.height+30<=rect.height<=grid.height+110:
                expected.add((rect.x,rect.y,rect.width,rect.height))
            ancestor=ancestor.parent
        return expected

    def activate_panel(self,panel):
        expected=self.panel_bounds(panel)
        pointer=self.ui.pointer();session=self.b.wechat_session()
        if not session:return False
        matches=[wid for wid,box in popup_windows(pointer) if box in expected and focus_owned(pointer,session[0],window_id=wid)]
        if len(matches)!=1:return False
        x=pointer.x11
        x.XRaiseWindow.argtypes=[ct.c_void_p,ct.c_ulong];x.XSetInputFocus.argtypes=[ct.c_void_p,ct.c_ulong,ct.c_int,ct.c_ulong]
        x.XSync.argtypes=[ct.c_void_p,ct.c_int]
        x.XRaiseWindow(pointer.display,matches[0]);x.XSetInputFocus(pointer.display,matches[0],2,0);x.XSync(pointer.display,False)
        return True

    def close(self):
        if self._panel is not None:
            if self.ui.composer is not None:
                rect=self.b.component_rect(self.ui.composer,self.ui.pyatspi)
                self.ui.pointer().tap(rect.x+rect.width-20,rect.y+15)
            self._panel=None

    def tiles(self,panel):
        result=[]
        bounds=self.b.component_rect(panel,self.ui.pyatspi)
        for obj in self.b.moments.walk(panel,3000):
            try:
                if obj.getRoleName()!='push button' or not obj.getState().contains(self.ui.pyatspi.STATE_SHOWING):continue
                rect=self.b.component_rect(obj,self.ui.pyatspi)
                if 36<=rect.width<=100 and 36<=rect.height<=100 and rect.x>=bounds.x and rect.y>=bounds.y and rect.x+rect.width<=bounds.x+bounds.width and rect.y+rect.height<=bounds.y+bounds.height:
                    if (rect.x,rect.y) not in {(r.x,r.y) for r in result}:result.append(rect)
            except Exception:continue
        return sorted(result,key=lambda r:(r.y,r.x))

    def capture(self,rect):
        # Only the known sticker tile is captured, in server memory; no chat export.
        pixel=self.ui.Gdk.pixbuf_get_from_window(self.ui.screen,rect.x,rect.y,rect.width,rect.height)
        if pixel is None:return None
        channels=pixel.get_n_channels();stride=pixel.get_rowstride();data=pixel.get_pixels()
        packed=b''.join(data[y*stride:y*stride+rect.width*channels] for y in range(rect.height))
        return Image.frombytes('RGBA' if channels==4 else 'RGB',(rect.width,rect.height),packed).convert('RGB')

    def templates(self,asset):
        raw=Path(asset['path']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=asset['id']:raise ValueError('sticker_asset_changed')
        with tempfile.TemporaryDirectory(prefix='sticker-match-') as root:
            paths=sticker_media.frames(raw,root)
            result=[]
            for path in paths:
                with Image.open(path) as image:result.append(image.convert('RGB').copy())
            return result

    def send(self,asset,guard,before_submit):
        if self.should_yield():return 'yield'
        try:
            if self.ui.composer.queryText().characterCount:return 'draft_present'
        except Exception:return 'draft_unverified'
        if asset['native_md5'] not in self.favorites():return 'favorite_missing'
        panel=self.open()
        if panel is None:
            self.close();return 'panel_missing'
        submitted=False
        try:
            templates=self.templates(asset)
            boxes=self.tiles(panel)
            images=[self.capture(rect) for rect in boxes]
            if any(image is None for image in images):return 'capture_missing'
            index=match_tile(templates,images)
            if index is None:return 'tile_unverified'
            session=self.b.wechat_session()
            if self.should_yield() or not session or not focus_owned(self.ui.pointer(),session[0]) or not guard() or not before_submit():return 'yield'
            if not self.b._click_rect(self.ui,boxes[index]):return 'unconfirmed'
            # Caller closes after receipt polling, so focus-out cannot cancel pending work.
            submitted=True
            return 'submitted'
        finally:
            if not submitted:self.close()

    def favorite_latest(self,asset,job,select_chat,before_submit):
        if self.should_yield():return 'yield'
        digest=asset['md5']
        row=favorite_source(asset,job)
        hashes=row['message'].get('sticker_hashes',[]) if row else []
        for native,aliases in self.favorite_records():
            if digest in aliases or native in hashes:return native
        if row is None:return 'source_not_latest'
        message=row['message']
        current=[m for m in self.reader.messages(job['username'],limit=60,source=row['source']) if not m['outgoing'] and (m.get('type',0)&65535) in (3,47)]
        if not current or current[-1]['local_id']!=message['local_id']:return 'source_not_latest'
        if not select_chat(self.ui,self.reader,job,self.b):return 'chat_unverified'
        bounds=self.b.component_rect(self.ui.right_list,self.ui.pyatspi)
        pointer=self.ui.pointer();pointer.move(bounds.x+bounds.width//2,bounds.y+bounds.height//2)
        for _ in range(8):
            if self.should_yield():return 'yield'
            pointer.scroll(5)
        time.sleep(.3)
        media=[]
        for attempt in range(5):
            if self.should_yield():return 'yield'
            media=[r for r in self.ui.rows(self.ui.right_list) if self.b.is_image_stub(r[0]) and r[1].y>=bounds.y and r[1].y+r[1].height<=bounds.y+bounds.height and self.ui.incoming_state(r[0],r[1])=='incoming']
            if media:break
            if attempt<4:pointer.scroll(3,up=True);time.sleep(.2)
        if not media:return 'source_unverified'
        bubble=max(media,key=lambda r:r[1].y)
        rect=self.b.image_capture_rect(self.ui,bubble[1],job['kind']=='group')
        x,y,w,h=rect if isinstance(rect,tuple) else (rect.x,rect.y,rect.width,rect.height)
        from types import SimpleNamespace
        image=self.capture(SimpleNamespace(x=x,y=y,width=w,height=h))
        if image is None:return 'source_image_unverified'
        if self.should_yield():return 'yield'
        templates=self.templates(asset)
        if self.should_yield():return 'yield'
        image=self.capture(SimpleNamespace(x=x,y=y,width=w,height=h))
        if image is None or match_tile(templates,[image],threshold=.12) is None:return 'source_image_unverified'
        current=[m for m in self.reader.messages(job['username'],limit=60,source=row['source']) if not m['outgoing'] and (m.get('type',0)&65535) in (3,47)]
        if not current or current[-1]['local_id']!=message['local_id']:return 'source_not_latest'
        pointer=self.ui.pointer();pointer.move(x+w//2,y+h//2)
        pointer.xtst.XTestFakeButtonEvent(pointer.display,3,True,0)
        pointer.xtst.XTestFakeButtonEvent(pointer.display,3,False,0)
        pointer.x11.XFlush(pointer.display);time.sleep(.3)
        options=[]
        for obj in self.b.moments.walk(self.ui.app,15000):
            try:
                if obj.getRoleName() in ('menu item','push button') and (obj.name or '').strip() in ('Add Sticker','Add to Stickers','Add to stickers','Add to Favorite Stickers','添加到表情','添加到我的表情') and obj.getState().contains(self.ui.pyatspi.STATE_SHOWING):
                    options.append(self.b.component_rect(obj,self.ui.pyatspi))
            except Exception:continue
        if len(options)!=1:
            if self.ui.composer:
                cr=self.b.component_rect(self.ui.composer,self.ui.pyatspi);pointer.tap(cr.x+cr.width-20,cr.y+15)
            return 'add_control_missing'
        if not before_submit():
            if self.ui.composer:
                cr=self.b.component_rect(self.ui.composer,self.ui.pyatspi);pointer.tap(cr.x+cr.width-20,cr.y+15)
            return 'yield'
        self.b._click_rect(self.ui,options[0])
        for _ in range(12):
            if self.should_yield():return 'unconfirmed'
            for native,aliases in self.favorite_records():
                if digest in aliases or native in hashes:return native
            time.sleep(.25)
        return 'unconfirmed'
