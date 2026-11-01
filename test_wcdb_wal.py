"""Authenticated synthetic pages; AES itself is unchanged and stubbed here."""
import hashlib,hmac,json,struct,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
import wechat_db as w

KEY=b'k'*32;SALT=b's'*16;IV=b'i'*16
MAC=hashlib.pbkdf2_hmac('sha512',KEY,bytes(v^0x3a for v in SALT),2,dklen=32)

def page(number,value):
    data=bytearray(value*(w.PAGE-w.RESERVE-(16 if number==1 else 0)))
    if number==1:data[12:16]=struct.pack('>I',2)
    data=bytes(data)
    signed=data+IV
    return (SALT if number==1 else b'')+signed+hmac.new(MAC,signed+struct.pack('<I',number),hashlib.sha512).digest()

def wal(frames):
    header=struct.pack('>6I',0x377f0682,3007000,w.PAGE,0,11,22)
    a=b=0
    values=struct.unpack('<6I',header)
    for i in range(0,6,2):a=(a+values[i]+b)&0xffffffff;b=(b+values[i+1]+a)&0xffffffff
    data=header+struct.pack('>II',a,b)
    for number,commit,value,current in frames:
        frame=struct.pack('>4I',number,commit,11 if current else 99,22);payload=page(number,value)
        values=struct.unpack('<'+str((8+len(payload))//4)+'I',frame[:8]+payload)
        for i in range(0,len(values),2):a=(a+values[i]+b)&0xffffffff;b=(b+values[i+1]+a)&0xffffffff
        data+=frame+struct.pack('>II',a,b)+payload
    return data

class WCDBWalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'source.db';self.target=self.root/'snapshot.db'
        self.source.write_bytes(page(1,b'A')+page(2,b'B'))
        self.log=Path(str(self.source)+'-wal')
        self.shm=Path(str(self.source)+'-shm')
        self.decrypt=patch('wechat_db._aes_decrypt',side_effect=lambda lib,key,iv,data:data)
        self.decrypt.start()

    def tearDown(self):self.decrypt.stop();self.temp.cleanup()

    def install_wal(self,frames,committed=None,pages=None,rewrite=True):
        data=wal(frames)
        if rewrite:self.log.write_bytes(data)
        if committed is None:
            committed=0
            for i,frame in enumerate(frames):
                if not frame[3]:break
                if frame[1]:committed=i+1;pages=frame[1]
        offset=32+(committed-1)*(w.PAGE+24)
        crc=struct.unpack('>II',data[offset+16:offset+24]) if committed else (0,0)
        header=bytearray(struct.pack('=IIIBBH8I',3007000,0,1,1,0,w.PAGE,committed,pages or 0,*crc,0,0,0,0))
        header[32:40]=data[16:24];a=b=0;values=struct.unpack('=10I',header[:40])
        for i in range(0,10,2):a=(a+values[i]+b)&0xffffffff;b=(b+values[i+1]+a)&0xffffffff
        header[40:48]=struct.pack('=II',a,b);self.shm.write_bytes(header+header)

    def test_applies_only_committed_frames_and_ignores_stale_generation(self):
        self.install_wal([(2,2,b'C',True),(2,0,b'D',True),(2,2,b'E',False)])
        w.decrypt_database(None,str(self.source),str(self.target),KEY)
        self.assertEqual(self.target.read_bytes()[w.PAGE:w.PAGE+32],b'C'*32)
        self.assertEqual(self.source.read_bytes()[w.PAGE:w.PAGE+32],b'B'*32)

    def test_bad_page_hmac_does_not_replace_existing_snapshot(self):
        self.install_wal([(2,2,b'C',True)])
        data=bytearray(self.log.read_bytes());data[-1]^=1;self.log.write_bytes(data)
        self.target.write_bytes(b'previous')
        with self.assertRaisesRegex(RuntimeError,'HMAC'):
            w.decrypt_database(None,str(self.source),str(self.target),KEY)
        self.assertEqual(self.target.read_bytes(),b'previous')

    def test_bad_wal_header_checksum_is_rejected(self):
        self.install_wal([(2,2,b'C',True)])
        data=bytearray(self.log.read_bytes());data[24]^=1;self.log.write_bytes(data)
        with self.assertRaisesRegex(RuntimeError,'WAL header'):
            w.decrypt_database(None,str(self.source),str(self.target),KEY)

    def test_cache_is_invalidated_by_wal_even_if_main_database_is_unchanged(self):
        reader=w.WeChatDB.__new__(w.WeChatDB);reader._lib=None;reader.db_dir=self.root
        reader.cache_dir=self.root/'cache';reader.keys={'source.db':{'enc_key':KEY.hex()}}
        original=w.decrypt_database
        with patch('wechat_db.decrypt_database',wraps=original) as decrypt:
            self.install_wal([(2,2,b'C',True)]);first=reader._cached('source.db')
            self.assertEqual(first.read_bytes()[w.PAGE:w.PAGE+16],b'C'*16)
            reader._cached('source.db');self.assertEqual(decrypt.call_count,1)
            self.install_wal([(2,2,b'D',True)]);second=reader._cached('source.db')
            self.assertEqual(second.read_bytes()[w.PAGE:w.PAGE+16],b'D'*16)
            self.assertEqual(decrypt.call_count,2)

    def test_connections_open_only_the_private_snapshot_read_only(self):
        reader=w.WeChatDB.__new__(w.WeChatDB);reader._cached=Mock(return_value=self.target)
        with patch('wechat_db.sqlite3.connect') as connect:
            reader._connect('source.db')
        connect.assert_called_once_with(self.target.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)

    def test_forged_uncommitted_frame_count_is_not_a_commit(self):
        self.install_wal([(2,2,b'C',True),(2,2,b'D',True)],committed=1,pages=2)
        w.decrypt_database(None,str(self.source),str(self.target),KEY)
        self.assertEqual(self.target.read_bytes()[w.PAGE:w.PAGE+16],b'C'*16)

    def test_grow_then_shrink_drops_pages_beyond_final_committed_size(self):
        self.install_wal([(3,3,b'C',True),(2,2,b'D',True)])
        w.decrypt_database(None,str(self.source),str(self.target),KEY)
        self.assertEqual(self.target.stat().st_size,2*w.PAGE)
        self.assertEqual(self.target.read_bytes()[w.PAGE:w.PAGE+16],b'D'*16)

    def test_torn_uncommitted_tail_does_not_hide_previous_commit(self):
        self.install_wal([(2,2,b'C',True),(2,0,b'D',True)])
        raw=bytearray(self.log.read_bytes());raw[-1]^=1;self.log.write_bytes(raw)
        w.decrypt_database(None,str(self.source),str(self.target),KEY)
        self.assertEqual(self.target.read_bytes()[w.PAGE:w.PAGE+16],b'C'*16)

    def test_corrupt_base_page_after_page_one_is_rejected(self):
        raw=bytearray(self.source.read_bytes());raw[-1]^=1;self.source.write_bytes(raw)
        with self.assertRaisesRegex(RuntimeError,'HMAC'):
            w.decrypt_database(None,str(self.source),str(self.target),KEY)

    def test_cache_observes_commit_published_only_in_shared_header(self):
        reader=w.WeChatDB.__new__(w.WeChatDB);reader._lib=None;reader.db_dir=self.root
        reader.cache_dir=self.root/'cache';reader.keys={'source.db':{'enc_key':KEY.hex()}}
        frames=[(2,2,b'C',True),(2,2,b'D',True)]
        self.install_wal(frames,committed=1,pages=2);first=reader._cached('source.db')
        self.assertEqual(first.read_bytes()[w.PAGE:w.PAGE+16],b'C'*16)
        original_stat=self.log.stat()
        self.install_wal(frames,committed=2,pages=2,rewrite=False)
        self.assertEqual(self.log.stat().st_mtime_ns,original_stat.st_mtime_ns)
        self.assertEqual(self.log.stat().st_ctime_ns,original_stat.st_ctime_ns)
        second=reader._cached('source.db')
        self.assertEqual(second.read_bytes()[w.PAGE:w.PAGE+16],b'D'*16)

    def test_changing_source_does_not_publish_snapshot(self):
        self.target.write_bytes(b'previous')
        versions=iter(['before','after','after'])
        with patch('wechat_db._snapshot_version',side_effect=lambda _:next(versions)):
            with self.assertRaises(w.SnapshotChanged):
                w.decrypt_database(None,str(self.source),str(self.target),KEY)
        self.assertEqual(self.target.read_bytes(),b'previous')

    def test_integrity_failure_during_native_write_is_retried_as_source_change(self):
        self.target.write_bytes(b'previous')
        with patch('wechat_db._snapshot_version',side_effect=['before','after']),patch('wechat_db._page_valid',side_effect=[True,True,False]):
            with self.assertRaises(w.SnapshotChanged):
                w.decrypt_database(None,str(self.source),str(self.target),KEY)
        self.assertEqual(self.target.read_bytes(),b'previous')

if __name__=='__main__':unittest.main()
