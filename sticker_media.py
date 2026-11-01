"""Read message-bound original stickers from the current account's local cache."""
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess

MAX_BYTES = 8 * 1024 * 1024
KEY_FILE = Path('/etc/wechat-autoreply/emoticon-key.json')


def metadata(text):
    if not isinstance(text,str) or len(text)>100_000:
        return []
    values = re.findall(r'\b(?:md5|androidmd5|externmd5)=["\']([a-fA-F0-9]{32})["\']', text)
    return list(dict.fromkeys(v.lower() for v in values))


def original_bytes(account_dir, account, hashes, key_file=KEY_FILE):
    hashes = [h.lower() for h in hashes if re.fullmatch('[a-fA-F0-9]{32}',h)]
    if not hashes:
        return None
    try:
        key_hex = json.loads(Path(key_file).read_text()).get(account,'')
        key = bytes.fromhex(key_hex) if re.fullmatch('[a-fA-F0-9]{32}',key_hex) else None
    except (OSError,ValueError,TypeError):
        key = None
    root = Path(account_dir).resolve()
    bases = [root/'business'/'emoticon'/name for name in ('Persist','PersistStore','Thumb','ThumbStore','Temp')]
    cache = root/'cache'
    if cache.is_dir():
        bases += [folder/'Emoticon' for folder in cache.iterdir() if re.fullmatch(r'\d{4}-\d{2}',folder.name)]
    for base in bases:
        for digest in hashes:
            for directory in (base/digest[:2],base):
                for suffix in ('','.gif','.png','.webp','.dat'):
                    path=directory/(digest+suffix)
                    try:
                        if not path.resolve().is_relative_to(root) or not path.is_file() or path.stat().st_size>MAX_BYTES:
                            continue
                        raw=path.read_bytes()
                        candidates=[raw]
                        if key and raw and len(raw)%16==0:
                            from Crypto.Cipher import AES
                            from Crypto.Util.Padding import unpad
                            try:
                                candidates.append(unpad(AES.new(key,AES.MODE_CBC,key).decrypt(raw),16))
                            except ValueError:
                                pass
                        for data in candidates:
                            if hashlib.md5(data).hexdigest() in hashes:
                                return data
                    except OSError:
                        continue
    return None


def ffmpeg_executable():
    installed=shutil.which('ffmpeg')
    if installed:
        return installed
    bundled=Path(__file__).parent/'media-tools'/'imageio_ffmpeg'/'binaries'
    return next((str(p) for p in bundled.glob('ffmpeg-linux*') if p.is_file()),None)


def meaningful(image):
    extrema=image.convert('RGB').getextrema()
    return any(high-low>12 or high<240 for low,high in extrema)


def frames(raw, out_dir):
    from PIL import Image
    if not raw or len(raw)>MAX_BYTES:
        return []
    root=Path(out_dir)
    root.mkdir(parents=True,exist_ok=True)
    if raw.startswith(b'wxgf'):
        tool=ffmpeg_executable()
        if not tool:
            return []
        source=root/'original.wxgf'
        source.write_bytes(raw)
        # Retain the container header: some files contain HEVC parameter sets before the first VPS.
        result=subprocess.run([tool,'-hide_banner','-loglevel','error','-nostdin','-threads','1','-max_alloc','67108864',
            '-max_pixels','2097152',
            '-f','hevc','-i',str(source),'-frames:v','24','-vf','scale=512:512:force_original_aspect_ratio=decrease',
            '-vsync','0',str(root/'decoded-%03d.png')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=12)
        candidates=sorted(root.glob('decoded-*.png'))
        if result.returncode or not candidates:
            return []
        source_images=[]
        for path in candidates:
            with Image.open(path) as image:
                if meaningful(image):
                    source_images.append(image.convert('RGB').copy())
    else:
        source_images=[]
        with Image.open(io.BytesIO(raw)) as image:
            pixels=image.width*image.height
            if pixels>8_000_000:
                return []
            for index in range(min(32,getattr(image,'n_frames',1),32_000_000//max(1,pixels))):
                image.seek(index)
                frame=image.convert('RGBA')
                background=Image.new('RGBA',frame.size,'white')
                background.alpha_composite(frame)
                frame=background.convert('RGB')
                frame.thumbnail((1024,1024))
                if meaningful(frame):
                    source_images.append(frame.copy())
    if not source_images:
        return []
    picks=list(dict.fromkeys((0,len(source_images)//2,len(source_images)-1)))
    output=[]
    seen=set()
    for index in picks:
        image=source_images[index]
        digest=hashlib.sha256(image.tobytes()).digest()
        if digest in seen:
            continue
        seen.add(digest)
        path=root/f'frame-{index}.png'
        image.save(path)
        output.append(str(path))
    return output
