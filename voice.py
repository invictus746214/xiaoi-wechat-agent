"""Voice reply: Breeze TTS + WeChat native voice bubble over the virtual mic."""
import asyncio
import os
import pathlib
import re
import subprocess
import threading
import time
import wave

import miniaudio

try:
    import edge_tts
except Exception:  # pragma: no cover
    edge_tts = None

FIFO = "/tmp/xiaoqi_mic_fifo"
STYLE = "playful"
VOICE_REQUEST = re.compile(
    r"(语音|(?:发|用|来|整|录).{0,2}语音|语音回复|语音说|语音介绍|语音回我|回我语音|"
    r"想听你.{0,4}声音|说给我听|别打字|直接说|用说的|开口说|"
    r"唱歌|唱两句|唱一段|唱首|唱个|来一首|点歌|唱给我听)",
    re.I,
)
VOICE_NEGATIVE = re.compile(
    r"(语音识别|语音输入|语音转文字|语音助手|语音通话|语音消息|语音条|语音包|"
    r"语音设置|语音权限|麦克风|录音)",
    re.I,
)
SING_REQUEST = re.compile(r"(唱|点歌|来一首|唱歌|来一段)", re.I)


def requested(text):
    value = text or ""
    if VOICE_NEGATIVE.search(value) and not re.search(r"(发|回|用|来|唱|说|念)", value):
        return False
    return bool(VOICE_REQUEST.search(value))


def sing_requested(text):
    return bool(SING_REQUEST.search(text or ""))


def _env():
    values = {}
    try:
        for line in pathlib.Path("/etc/wechat-autoreply/breeze.env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                name, value = line.split("=", 1)
                values[name.strip()] = value.strip()
    except OSError:
        pass
    return values


def _breeze_wav(text, out):
    from breeze_blue import BreezeBlue

    env = _env()
    key = env.get("BREEZE_API_KEY") or os.environ.get("BREEZE_API_KEY")
    voice_id = env.get("BREEZE_VOICE_ID") or os.environ.get("BREEZE_VOICE_ID")
    if not key or not voice_id:
        return None
    client = BreezeBlue(api_key=key)
    audio = client.text_to_speech.convert(
        voice_id, text=text, instructions="活泼、俏皮，带一点笑意，语速稍快，不要客服腔。",
        output_format="mp3",
    )
    mp3 = out + ".mp3"
    pathlib.Path(mp3).write_bytes(audio.content)
    decoded = miniaudio.decode_file(
        mp3, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=48000
    )
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(decoded.sample_rate)
        handle.writeframes(decoded.samples.tobytes())
    return out


def _edge_wav(text, out):
    if edge_tts is None:
        return None

    async def run():
        await edge_tts.Communicate(
            text, "zh-CN-liaoning-XiaobeiNeural", rate="-4%", pitch="+1Hz"
        ).save(out + ".mp3")

    asyncio.run(run())
    decoded = miniaudio.decode_file(
        out + ".mp3", output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=48000
    )
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(decoded.sample_rate)
        handle.writeframes(decoded.samples.tobytes())
    return out


def clean_spoken(text):
    """Drop self-denials and fake '(语音)' prefixes from a spoken reply."""
    value = re.sub(r"^\s*[（(]\s*语音\s*[）)]\s*", "", text or "")
    parts = re.split(r"(?<=[。！？!?])", value)
    kept = [part for part in parts if not re.search(
        r"(发不了语音|没.{0,3}嗓子|只能打字|不能说话|说不了话|没有语音)", part)]
    cleaned = "".join(kept).strip()
    return cleaned or "好呀，我在呢。"


def synthesize(text, out="/tmp/xiaoqi-reply.wav"):
    cleaned = re.sub(r"```.*?```", "", text or "", flags=re.S)
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    cleaned = re.sub(r"[*_#>`~]", "", cleaned).strip()
    if len(cleaned) > 140:
        cleaned = cleaned[:140]
    try:
        result = _breeze_wav(cleaned, out)
    except Exception:
        result = None
    return result or _edge_wav(cleaned, out)


def ensure_mic():
    try:
        sources = subprocess.check_output(["pactl", "list", "short", "sources"], text=True, timeout=5)
    except Exception:
        sources = ""
    if "xiaoqi_pipe_mic" not in sources:
        subprocess.run(["pactl", "load-module", "module-pipe-source", "source_name=xiaoqi_pipe_mic",
                        f"file={FIFO}", "format=s16le", "rate=48000", "channels=1"],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["pactl", "set-default-source", "xiaoqi_pipe_mic"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not os.path.exists(FIFO):
        try:
            os.mkfifo(FIFO, 0o600)
        except OSError:
            pass


def _feed(wav_path):
    with wave.open(wav_path, "rb") as source:
        frames = source.readframes(source.getnframes())
    with open(FIFO, "wb", buffering=0) as pipe:
        for offset in range(0, len(frames), 9600):
            pipe.write(frames[offset:offset + 9600])
            time.sleep(0.1)


def _button_rect(ui, name):
    import moments
    for obj in moments.walk(ui.app, 60000):
        try:
            if (obj.name or "").strip() == name and obj.getState().contains(ui.pyatspi.STATE_SHOWING):
                from bridge import component_rect
                return component_rect(obj, ui.pyatspi)
        except Exception:
            continue
    return None


def send(ui, wav_path, guard=None):
    """Send a native voice bubble in the currently selected chat."""
    ensure_mic()
    button = _button_rect(ui, "Send Voice")
    if button is None:
        return False
    ui.activate()
    if guard and not guard():
        return False
    ui.pointer().tap(button.x + button.width // 2, button.y + button.height // 2)
    time.sleep(0.5)
    worker = threading.Thread(target=_feed, args=(wav_path,), daemon=True)
    worker.start()
    worker.join(timeout=20)
    if worker.is_alive():
        raise RuntimeError("voice_feed_timeout")
    if guard and not guard():
        raise RuntimeError("voice_chat_changed")
    time.sleep(0.2)
    ui.pointer().tap(button.x + 64, button.y + 12)
    return True
