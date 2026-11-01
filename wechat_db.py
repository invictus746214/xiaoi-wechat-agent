"""Read-only WeChat Linux database reader (WCDB / SQLCipher4).

Decrypts the live DB files into a private cache and exposes sessions, messages,
contacts and Moments.  It never writes to WeChat's own files.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import glob
import hashlib
import hmac
import json
import os
import pathlib
import re
import sqlite3
import struct
import tempfile
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
import threading

try:
    import zstandard
except Exception:  # pragma: no cover - optional dependency
    zstandard = None

try:
    from sqlcipher3 import dbapi2 as sqlcipher
except Exception:  # pragma: no cover - optional dependency
    sqlcipher = None

try:
    from Crypto.Cipher import AES as _AES
except Exception:  # pragma: no cover - optional dependency
    _AES = None


def _aes_ecb_decrypt(key, data):
    if _AES is None:
        raise RuntimeError("pycryptodome not installed")
    return _AES.new(key, _AES.MODE_ECB).decrypt(data)


def _pkcs7_unpad(data):
    if not data:
        return data
    pad = data[-1]
    return data[:-pad] if 0 < pad <= 16 else data

PAGE = 4096
KEY = 32
SALT = 16
IV = 16
HMAC = 64
RESERVE = 80

DEFAULT_KEYS = "/etc/wechat-autoreply/wcdb-keys.json"
DEFAULT_CACHE = "/var/lib/wechat-autoreply/db-cache"
IMAGE_KEY_FILE = "/etc/wechat-autoreply/image-key.json"


def _load_crypto():
    lib = ctypes.CDLL(ctypes.util.find_library("crypto"))
    lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
    lib.EVP_aes_256_cbc.restype = ctypes.c_void_p
    lib.EVP_DecryptInit_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_char_p, ctypes.c_char_p]
    lib.EVP_DecryptUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.POINTER(ctypes.c_int), ctypes.c_char_p, ctypes.c_int]
    lib.EVP_DecryptFinal_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_int)]
    lib.EVP_CIPHER_CTX_set_padding.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]
    return lib


def _aes_decrypt(lib, key, iv, data):
    ctx = lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise RuntimeError("EVP_CIPHER_CTX_new failed")
    try:
        if lib.EVP_DecryptInit_ex(ctx, lib.EVP_aes_256_cbc(), None,
                                  ctypes.c_char_p(key), ctypes.c_char_p(iv)) != 1:
            raise RuntimeError("EVP_DecryptInit_ex failed")
        lib.EVP_CIPHER_CTX_set_padding(ctx, 0)
        out = ctypes.create_string_buffer(len(data) + 32)
        out_len = ctypes.c_int(0)
        if lib.EVP_DecryptUpdate(ctx, out, ctypes.byref(out_len),
                                 ctypes.c_char_p(data), len(data)) != 1:
            raise RuntimeError("EVP_DecryptUpdate failed")
        final = ctypes.create_string_buffer(32)
        final_len = ctypes.c_int(0)
        lib.EVP_DecryptFinal_ex(ctx, final, ctypes.byref(final_len))
        return out.raw[:out_len.value + final_len.value]
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


def _verify(page1, key):
    salt = page1[:SALT]
    mac_salt = bytes(value ^ 0x3A for value in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, dklen=KEY)
    return _page_valid(page1,mac_key,1)


def _page_valid(page,mac_key,number):
    start=SALT if number==1 else 0
    signed=page[start:PAGE-HMAC]+struct.pack('<I',number)
    return len(page)==PAGE and hmac.compare_digest(hmac.new(mac_key,signed,hashlib.sha512).digest(),page[-HMAC:])


def _checksum(data,order):
    a=b=0;words=struct.unpack(order+str(len(data)//4)+'I',data)
    for i in range(0,len(words),2):
        a=(a+words[i]+b)&0xffffffff;b=(b+words[i+1]+a)&0xffffffff
    return a,b


class SnapshotChanged(RuntimeError):
    pass


_CACHE_LOCK = threading.RLock()


def _snapshot_version(src):
    values=[]
    for path in (src, str(src)+'-wal'):
        try:
            stat=os.stat(path)
            values.append((stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns))
        except FileNotFoundError:values.append(None)
    try:
        with open(str(src)+'-shm','rb') as handle:values.append(handle.read(96))
    except FileNotFoundError:values.append(None)
    return hashlib.sha256(repr(values).encode()).hexdigest()


@contextmanager
def _cache_guard(path):
    with _CACHE_LOCK, open(str(path)+'.lock','a+b') as handle:
        os.chmod(handle.name,0o600)
        if os.name!='nt':
            import fcntl
            fcntl.flock(handle,fcntl.LOCK_EX)
        try:yield
        finally:
            if os.name!='nt':fcntl.flock(handle,fcntl.LOCK_UN)


def _overlay_wal(lib,src,target,key,salt,total):
    try:log=open(str(src)+'-wal','rb')
    except FileNotFoundError:return
    with log:
        header=log.read(32)
        if len(header)<32:return
        magic,version,size=struct.unpack('>III',header[:12])
        if magic not in (0x377f0682,0x377f0683) or version!=3007000 or size!=PAGE:
            raise RuntimeError('invalid WAL header')
        if _checksum(header[:24],'<' if magic==0x377f0682 else '>')!=struct.unpack('>II',header[24:32]):
            raise RuntimeError('invalid WAL header checksum')
        try:
            with open(str(src)+'-shm','rb') as index:shared=index.read(96)
        except FileNotFoundError:raise RuntimeError('WAL committed index unavailable') from None
        if len(shared)!=96 or shared[:48]!=shared[48:]:raise SnapshotChanged('WAL committed index changed')
        fields=struct.unpack('=IIIBBH8I',shared[:48])
        if (fields[0]!=3007000 or fields[3]!=1 or fields[4]!=(magic&1) or fields[5]!=PAGE
                or shared[32:40]!=header[16:24] or _checksum(shared[:40],'=')!=fields[12:14]):
            raise SnapshotChanged('invalid WAL committed index')
        committed,pages=fields[6:8]
        if committed>(os.fstat(log.fileno()).st_size-32)//(PAGE+24) or pages>0x7fffffff:
            raise SnapshotChanged('WAL committed index outside available log')
        if committed and not pages:raise RuntimeError('invalid committed WAL size')
        mac_key=hashlib.pbkdf2_hmac('sha512',key,bytes(v^0x3a for v in salt),2,dklen=KEY)
        frames=[]
        for i in range(committed):
            frame=log.read(24)
            if len(frame)<24 or frame[8:16]!=header[16:24]:raise SnapshotChanged('WAL generation changed')
            position=log.tell();data=log.read(PAGE)
            if len(data)<PAGE:raise SnapshotChanged('WAL committed frame incomplete')
            number,count=struct.unpack('>II',frame[:8])
            if not 0<number<=0x7fffffff or count>0x7fffffff:raise RuntimeError('invalid WAL page number')
            if not _page_valid(data,mac_key,number):
                raise RuntimeError('WAL page HMAC mismatch')
            frames.append((number,position))
            if i==committed-1 and (count!=pages or struct.unpack('>II',frame[16:24])!=fields[8:10]):
                raise SnapshotChanged('WAL commit does not match native index')
        # The live writer publishes SHM only at commit; never infer commits from frame counts alone.
        for number,position in frames:
            if number>pages:continue
            log.seek(position);data=log.read(PAGE);start=SALT if number==1 else 0
            plain=_aes_decrypt(lib,key,data[PAGE-RESERVE:PAGE-RESERVE+IV],data[start:PAGE-RESERVE])
            target.seek((number-1)*PAGE)
            target.write((b'SQLite format 3\x00' if number==1 else b'')+plain+b'\x00'*RESERVE)
        if committed:target.truncate(pages*PAGE)


def decrypt_database(lib, src, dst, key):
    before=_snapshot_version(src)
    with open(src, "rb") as handle:
        page1 = handle.read(PAGE)
    if len(page1) < PAGE or not _verify(page1, key):
        if _snapshot_version(src)!=before:raise SnapshotChanged('native database changed during snapshot')
        raise RuntimeError("database HMAC mismatch: " + src)
    first_plain=_aes_decrypt(lib,key,page1[PAGE-RESERVE:PAGE-RESERVE+IV],page1[SALT:PAGE-RESERVE])
    total=struct.unpack('>I',first_plain[12:16])[0] or os.path.getsize(src)//PAGE
    if not 1<=total<=os.path.getsize(src)//PAGE:raise RuntimeError('truncated database pages')
    mac_key=hashlib.pbkdf2_hmac('sha512',key,bytes(v^0x3a for v in page1[:SALT]),2,dklen=KEY)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=os.path.dirname(dst), suffix=".tmp")
    os.close(fd)
    try:
        with open(src, "rb") as source, open(temporary, "wb") as target:
            for number in range(1, total + 1):
                page = source.read(PAGE)
                if not _page_valid(page,mac_key,number):raise RuntimeError('database page HMAC mismatch')
                if number == 1:
                    plain = _aes_decrypt(lib, key, page[PAGE - RESERVE:PAGE - RESERVE + IV],
                                         page[SALT:PAGE - RESERVE])
                    target.write(b"SQLite format 3\x00" + plain + b"\x00" * RESERVE)
                else:
                    plain = _aes_decrypt(lib, key, page[PAGE - RESERVE:PAGE - RESERVE + IV],
                                         page[:PAGE - RESERVE])
                    target.write(plain + b"\x00" * RESERVE)
            _overlay_wal(lib,src,target,key,page1[:SALT],total)
        if _snapshot_version(src)!=before:raise SnapshotChanged('native database changed during snapshot')
        os.replace(temporary, dst)
        return before
    except RuntimeError:
        if _snapshot_version(src)!=before:raise SnapshotChanged('native database changed during snapshot') from None
        raise
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _text(value):
    if isinstance(value, bytes):
        for encoding in ("utf-8", "gb18030"):
            try:
                return value.decode(encoding)
            except UnicodeDecodeError:
                continue
        return value.decode("utf-8", "replace")
    return str(value or "")


def _xml_text(value):
    raw = _text(value).strip()
    if not raw.startswith("<"):
        return raw
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return raw
    for tag in ("title", "contentDesc", "des"):
        node = root.find(".//" + tag)
        if node is not None and (node.text or "").strip():
            return (node.text or "").strip()
    return raw


def quote_metadata(value,sender):
    text=_text(value).strip()
    for prefix in (sender+':\n',sender+'：\n',sender+'\n'):
        if sender and text.startswith(prefix):
            text=text[len(prefix):].strip()
            break
    if len(text)>100_000 or '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
        return None
    try:
        root=ET.fromstring(text)
        app=root if root.tag=='appmsg' else root.find('appmsg') if root.tag=='msg' else None
        if app is None or app.findtext('type')!='57':
            return None
        ref=app.find('refermsg')
        if ref is None:
            return None
        source=ref.findtext('fromusr','')
        quoted=ref.findtext('chatusr','') if source.endswith('@chatroom') else source or ref.findtext('chatusr','')
        content=ref.findtext('content','')[:300] if ref.findtext('type')=='1' else '[引用非文字消息，内容未读取]'
        return dict(text=app.findtext('title','')[:1000],quote_sender=quoted[:128],quote_text=content,
                    quote_server_id=ref.findtext('svrid','')[:32])
    except ET.ParseError:
        return None


class WeChatDB:
    def __init__(self, db_dir=None, keys_file=DEFAULT_KEYS, cache_dir=DEFAULT_CACHE):
        self.db_dir = pathlib.Path(db_dir) if db_dir else self._find_db_dir()
        self.account = re.sub(r"_[0-9a-f]{4}$", "", self.db_dir.parent.name)
        self.keys_file = pathlib.Path(keys_file)
        self.cache_dir = pathlib.Path(cache_dir)
        self._lib = _load_crypto()
        self.keys = json.loads(self.keys_file.read_text(encoding="utf-8"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _find_db_dir():
        matches = glob.glob("/home/*/xwechat_files/*/db_storage")
        if not matches:
            raise RuntimeError("wechat db_storage not found")
        return pathlib.Path(matches[0])

    def _cached(self, rel):
        source = self.db_dir / rel
        target = self.cache_dir / rel
        if not source.exists():
            raise FileNotFoundError(source)
        info = self.keys.get(rel)
        if not info:
            raise KeyError("no key for " + rel)
        target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        stamp=pathlib.Path(str(target)+'.snapshot')
        with _cache_guard(target):
            try:cached=stamp.read_text(encoding='ascii')
            except OSError:cached=''
            if target.exists() and cached==_snapshot_version(source):return target
            for attempt in range(3):
                try:
                    version=decrypt_database(self._lib,str(source),str(target),bytes.fromhex(info['enc_key']))
                    break
                except SnapshotChanged:
                    if attempt==2:raise
            stamp.write_text(version,encoding='ascii');stamp.chmod(0o600)
        return target

    def _connect(self, rel):
        return sqlite3.connect(self._cached(rel).resolve().as_uri()+'?mode=ro&immutable=1',uri=True)

    def contacts(self):
        result = {}
        try:
            db = self._connect("contact/contact.db")
            for username, remark, nick, alias in db.execute(
                "SELECT username, remark, nick_name, alias FROM contact"
            ):
                result[username] = (remark or nick or alias or username)
            db.close()
        except Exception:
            pass
        return result

    def sessions(self, limit=50):
        names = self.contacts()
        db = self._connect("session/session.db")
        rows = db.execute(
            "SELECT username, unread_count, summary, last_timestamp, last_sender_display_name "
            "FROM SessionTable ORDER BY last_timestamp DESC LIMIT ?", (int(limit),)
        ).fetchall()
        db.close()
        output = []
        for username, unread, summary, stamp, sender in rows:
            if not username:
                continue
            output.append({
                "username": username,
                "title": names.get(username, username),
                "unread": int(unread or 0),
                "summary": _text(summary)[:200],
                "last_timestamp": int(stamp or 0),
                "last_sender": _text(sender),
            })
        return output

    def messages(self, username, limit=30, after_id=None, source="message/message_0.db"):
        table = "Msg_" + hashlib.md5(username.encode()).hexdigest()
        if not re.fullmatch(r"message/message_\d+\.db", source):
            raise ValueError("invalid_message_source")
        db = self._connect(source)
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            db.close()
            return []
        names = {row[0]: row[1] for row in db.execute("SELECT rowid, user_name FROM Name2Id")}
        where = "WHERE local_id > ?" if after_id is not None else ""
        order = "ASC" if after_id is not None else "DESC"
        params = (int(after_id), int(limit)) if after_id is not None else (int(limit),)
        rows = db.execute(
            f"SELECT local_id, local_type, real_sender_id, create_time, message_content, server_id "
            f"FROM {table} {where} ORDER BY local_id {order} LIMIT ?", params
        ).fetchall()
        db.close()
        output = []
        for local_id, local_type, sender_id, created, content, server_id in (rows if after_id is not None else reversed(rows)):
            raw = content if isinstance(content, bytes) else _text(content).encode("utf-8")
            if zstandard is not None and raw[:4] == b"\x28\xb5\x2f\xfd":
                try:
                    raw = zstandard.ZstdDecompressor().decompress(raw, max_output_size=2_000_000)
                except Exception:
                    pass
            text = _xml_text(raw)
            sender = names.get(sender_id, str(sender_id))
            quote=quote_metadata(raw,sender) if (int(local_type or 0)&65535)==49 else None
            if quote:
                text=quote['text']
            image_md5 = ""
            sticker_hashes = []
            if (int(local_type or 0) & 65535) == 47:
                from sticker_media import metadata
                sticker_hashes = metadata(text)
            if int(local_type or 0) == 3:
                match = re.search(r'md5="([0-9a-fA-F]{32})"', text)
                if match:
                    image_md5 = match.group(1).lower()
            output.append({
                "local_id": int(local_id),
                "type": 1 if quote else int(local_type or 1),
                "raw_type": int(local_type or 1),
                "sender": sender,
                "timestamp": int(created or 0),
                "text": text[:1000],
                "outgoing": sender == self.account,
                "image_md5": image_md5,
                "server_id": str(server_id or "0"),
                "source": source,
                "sticker_hashes": sticker_hashes,
                **({k:v for k,v in quote.items() if k!='text'} if quote else {}),
            })
        return output

    def image_key(self):
        try:
            data = json.loads(pathlib.Path(IMAGE_KEY_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, 0
        return data.get(self.account), int(data.get("xor_key") or 0)

    def image_path(self, image_md5, server_id=None):
        if not re.fullmatch(r'[0-9a-fA-F]{32}',str(image_md5 or '')):
            return None
        hashes=[]
        if server_id not in (None,'',0,'0'):
            value=str(server_id)
            if not re.fullmatch(r'[1-9][0-9]{0,18}',value) or int(value)>9223372036854775807:
                return None
            conn=None
            try:
                conn=self._connect('message/message_resource.db')
                rows=conn.execute('SELECT packed_info FROM MessageResourceInfo WHERE message_svr_id=? LIMIT 2',(int(value),)).fetchall()
                if len(rows)==1 and isinstance(rows[0][0],(bytes,bytearray,memoryview)) and len(rows[0][0])<=65536:
                    # The message's resource hash names its cache file; the XML MD5 may name a different encoding.
                    hashes=[item.decode().lower() for item in re.findall(rb'(?<![0-9a-fA-F])([0-9a-fA-F]{32})(?![0-9a-fA-F])',bytes(rows[0][0]))]
            except Exception:
                pass
            finally:
                if conn is not None:
                    conn.close()
        hashes.append(image_md5.lower())
        base=self.db_dir.parent/'msg'/'attach'
        for suffix in ('','_h','_t'):
            for digest in dict.fromkeys(hashes):
                matches=glob.glob(str(base/'*'/'*'/'Img'/(digest+suffix+'.dat')))
                matches=[path for path in matches if pathlib.Path(path).is_file() and pathlib.Path(path).resolve().is_relative_to(base.resolve())]
                if matches:
                    return matches[0]
        return None

    def decode_image(self, image_md5, out_dir=None, server_id=None):
        source=self.image_path(image_md5,server_id=server_id)
        if not source:
            return None
        aes_key, xor_key = self.image_key()
        if not aes_key:
            return None
        with open(source, "rb") as handle:
            data = handle.read()
        if data[:6] != b"\x07\x08V2\x08\x07" or len(data) < 31:
            return None
        aes_size, xor_size = struct.unpack_from("<LL", data, 6)
        aligned = aes_size + 16 if aes_size % 16 else aes_size + 16
        try:
            plain = _aes_ecb_decrypt(aes_key.encode("ascii"), data[15:15 + aligned])
            plain = _pkcs7_unpad(plain)
        except Exception:
            return None
        offset = 15 + aligned
        raw_end = len(data) - xor_size
        raw = data[offset:raw_end] if offset < raw_end else b""
        tail = bytes(value ^ xor_key for value in data[raw_end:])
        decoded = plain + raw + tail
        kind = "jpg" if decoded[:3] == b"\xff\xd8\xff" else "bin"
        target_dir = pathlib.Path(out_dir) if out_dir else self.cache_dir / "images"
        target_dir.mkdir(parents=True, exist_ok=True)
        output = target_dir / (image_md5 + "." + kind)
        output.write_bytes(decoded)
        return str(output)

    def moments(self, limit=20):
        db = self._connect("sns/sns.db")
        rows = db.execute("SELECT tid, content FROM SnsTimeLine ORDER BY rowid DESC LIMIT ?",
                          (int(limit),)).fetchall()
        interactions = {}
        for feed_id, kind, from_user, from_nick, content in db.execute(
            "SELECT feed_id, type, from_username, from_nickname, content FROM SnsMessage_tmp3"
        ):
            interactions.setdefault(str(feed_id), []).append(
                (int(kind or 0), from_user or "", from_nick or "", content or "")
            )
        db.close()
        output = []
        for tid, content in rows:
            raw = _text(content)
            try:
                root = ET.fromstring(raw)
            except ET.ParseError:
                continue
            for item in root.iter("TimelineObject"):
                username = (item.findtext("username") or "").strip()
                text = (item.findtext("contentDesc") or "").strip()
                created = int(item.findtext("createTime") or 0)
                post_id = (item.findtext("id") or "").strip()
                likes, comments = [], []
                for kind, from_user, from_nick, body in interactions.get(str(tid), []):
                    if kind == 2 or body.strip():
                        comments.append({
                            "username": from_user,
                            "nickname": from_nick,
                            "content": body.strip(),
                        })
                    else:
                        likes.append(from_user or from_nick)
                images = item.findall(".//mediaList/media")
                for node in root.findall('.//like_user_list/user_comment'):
                    liker=node.findtext('username','').strip()
                    if liker and node.findtext('b_deleted','0') in ('','0') and liker not in likes:
                        likes.append(liker)
                thread=[]
                for node in root.findall('.//comment_user_list/user_comment'):
                    identifier=node.findtext('comment_64id','0')
                    ref=node.findtext('ref_comment_64id','0')
                    identifier=identifier if identifier not in ('','0') else node.findtext('comment_id','')
                    ref=ref if ref not in ('','0') else node.findtext('ref_comment_id','0')
                    kind='emoji' if node.findtext('comment_emojiinfo_count','0') not in ('','0') else 'image' if node.findtext('comment_imageinfo_count','0') not in ('','0') else 'text'
                    body=node.findtext('content','').strip()
                    if not body and kind=='emoji':body='[表情评论：'+(node.findtext('.//emojiinfo/desc','').strip()[:80] or '未读取画面')+']'
                    if not body and kind=='image':body='[图片评论，未读取画面]'
                    stamp=node.findtext('create_time','0')
                    thread.append(dict(id=identifier,username=node.findtext('username',''),nickname=node.findtext('nickname',''),text=body[:500],
                        ref_id=ref,kind=kind,timestamp=int(stamp) if stamp.isdecimal() else 0,deleted=node.findtext('b_deleted','0') not in ('','0')))
                output.append({
                    "id": post_id,
                    "tid": str(tid),
                    "username": username,
                    "text": text[:1000],
                    "timestamp": created,
                    "likes": likes,
                    "comments": comments,
                    "thread": thread,
                    "image_count": len(images),
                })
        return output
