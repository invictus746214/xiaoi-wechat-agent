"""Account/message-bound, local-only incoming SILK transcription.

Optional deployment: pilk + vosk in voice_input.python_executable, and an
explicitly installed Vosk model. No automatic downloads or remote ASR calls.
"""
import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time


MAX_BYTES = 2_000_000
DEFAULT_MODEL = '/var/lib/wechat-autoreply/asr/vosk-model-small-cn-0.22'
_LOCK = threading.Lock()
MAX_RSS_KIB = 384 * 1024
WORKER_PHASES = ('imports', 'decode', 'model', 'recognition')


@contextmanager
def _phase(callback, name):
    start = time.monotonic()
    try:
        yield
    finally:
        if callback is not None:
            try:
                callback(name, max(0.0, time.monotonic() - start))
            except Exception:
                pass  # Diagnostics must not turn a verified transcript into failure.


def _model_ready(options):
    model = Path(options.get('model_path') or DEFAULT_MODEL)
    return all((model / name).is_file() for name in
               ('am/final.mdl', 'conf/model.conf', 'graph/HCLr.fst', 'graph/Gr.fst'))


def _worker_phases(scratch, callback):
    if callback is None:
        return
    try:
        with (Path(scratch) / 'phases.json').open('rb') as handle:
            data = handle.read(2049)
        if len(data) > 2048:
            return
        phases = json.loads(data)
        if (not isinstance(phases, dict) or not phases.keys() <= set(WORKER_PHASES)
                or any(type(value) not in (int, float) or not 0 <= value <= 15
                       for value in phases.values())):
            return
        for name in WORKER_PHASES:
            if name in phases:
                try:
                    callback(name, float(phases[name]))
                except Exception:
                    pass
    except (OSError, ValueError, TypeError, RecursionError):
        pass


def _options(cfg):
    options = cfg.get('voice_input', {})
    return options if isinstance(options, dict) else {}


def _bounded(value, default, upper):
    try:
        return max(1, min(upper, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _worker_env():
    return {**os.environ, 'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1',
            'MKL_NUM_THREADS': '1','MALLOC_ARENA_MAX':'2'}


def available(cfg):
    """Check explicit opt-in, installed model and local worker dependencies."""
    options = _options(cfg)
    if options.get('enabled') is not True:
        return False
    try:
        if not _model_ready(options):
            return False
        result = subprocess.run(
            [str(options.get('python_executable') or sys.executable), '-I', '-c', 'import pilk, vosk'],
            env=_worker_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3, check=False)
        return result.returncode == 0
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return False


def _valid_silk(raw, max_seconds):
    if not isinstance(raw, bytes) or not 12 <= len(raw) <= MAX_BYTES:
        return False
    offset = 10 if raw.startswith(b'\x02#!SILK_V3') else 9 if raw.startswith(b'#!SILK_V3') else 0
    if not offset:
        return False
    frames = 0
    while offset + 2 <= len(raw):
        length = struct.unpack_from('<h', raw, offset)[0]
        offset += 2
        if length == -1:
            return frames > 0 and offset == len(raw)
        if length <= 0 or length > 1250 or offset + length > len(raw):
            return False
        offset += length
        frames += 1
        # Packets contain at least 20 ms; decoded PCM checks the actual duration.
        if frames > max_seconds * 50:
            return False
    return frames > 0 and offset == len(raw)


def _voice_bytes(reader, message):
    """Revalidate original source and join a unique native voice record."""
    account = str(getattr(reader, 'account', '') or '')
    chat = message.get('chat_username')
    source = message.get('source', '')
    sid = str(message.get('server_id', ''))
    if (not account or message.get('account') != account or not isinstance(chat, str)
            or not 1 <= len(chat) <= 256 or message.get('outgoing') is not False
            or message.get('sender') == account
            or message.get('type') != 34 or (int(message.get('raw_type', 34)) & 65535) != 34
            or not re.fullmatch(r'message/message_[0-9]+\.db', source)
            or not re.fullmatch(r'[1-9][0-9]{0,18}', sid) or int(sid) > 9223372036854775807):
        return None
    local_id = int(message.get('local_id', 0))
    timestamp = int(message.get('timestamp', 0))
    if local_id <= 0 or timestamp <= 0:
        return None
    table = 'Msg_' + hashlib.md5(chat.encode()).hexdigest()
    connection = reader._connect(source)
    try:
        rows = connection.execute(
            f'SELECT m.local_type,m.create_time,n.user_name FROM {table} m '
            'LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id '
            'WHERE m.local_id=? AND m.server_id=? LIMIT 2', (local_id, int(sid))).fetchall()
    finally:
        connection.close()
    if (len(rows) != 1 or (int(rows[0][0]) & 65535) != 34
            or rows[0][1] != timestamp or rows[0][2] != message.get('sender')
            or not rows[0][2] or rows[0][2] == account):
        return None
    media_sources = sorted(name for name in reader.keys
                           if re.fullmatch(r'message/media_[0-9]+\.db', name))
    if not media_sources or len(media_sources) > 16:
        return None
    found = []
    for name in media_sources:
        connection = reader._connect(name)
        try:
            # Check size before selecting BLOB data into Python memory.
            records = connection.execute(
                'SELECT v.rowid,length(v.voice_data) FROM VoiceInfo v '
                'JOIN Name2Id n ON n.rowid=v.chat_name_id '
                'WHERE n.user_name=? AND v.svr_id=? AND v.local_id=? AND v.create_time=? LIMIT 2',
                (chat, int(sid), local_id, timestamp)).fetchall()
            for rowid, size in records:
                if not size or size > MAX_BYTES or found:
                    return None
                found.append(connection.execute('SELECT voice_data FROM VoiceInfo WHERE rowid=?', (rowid,)).fetchone()[0])
        finally:
            connection.close()
    return found[0] if len(found) == 1 else None


def transcribe_message(reader, message, cfg, *, phase_callback=None):
    """Return local ASR text or ''. Optional callback(name, seconds) contains no content.

    Fixed names: native_read, available_probe, imports, decode, model,
    recognition, worker, total. A failed child may report only completed phases.
    """
    with _phase(phase_callback, 'total'):
        return _transcribe_message(reader, message, cfg, phase_callback)


def _transcribe_message(reader, message, cfg, phase_callback):
    if _options(cfg).get('enabled') is not True or not _LOCK.acquire(blocking=False):
        return ''
    try:
        with _phase(phase_callback, 'native_read'):
            raw = _voice_bytes(reader, message)
        options = _options(cfg)
        seconds = _bounded(options.get('max_seconds', 60), 60, 60)
        if not _valid_silk(raw, seconds):
            return ''
        # The disposable worker imports dependencies; probing them first loaded twice.
        with _phase(phase_callback, 'available_probe'):
            if not _model_ready(options):
                return ''
        # The parent owns scratch so a killed native decoder leaves no audio files.
        with tempfile.TemporaryDirectory(prefix='xiaoi-asr-') as scratch:
            try:
                with _phase(phase_callback, 'worker'):
                    result = subprocess.run(
                        [str(options.get('python_executable') or sys.executable), '-I', str(Path(__file__).resolve()),
                         str(Path(options.get('model_path') or DEFAULT_MODEL).resolve()), str(seconds), scratch],
                        input=raw, env=_worker_env(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        timeout=_bounded(options.get('timeout', 15), 15, 15), check=False)
            finally:
                _worker_phases(scratch, phase_callback)
        if result.returncode != 0 or len(result.stdout) > 16384:
            return ''
        text = json.loads(result.stdout).get('text')
        return text.strip()[:2000] if isinstance(text, str) else ''
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, subprocess.SubprocessError):
        return ''
    finally:
        _LOCK.release()


def _recognize(raw, model_path, seconds, scratch, phase_callback=None):
    """Run only inside the disposable, memory/time/file-size limited child."""
    with _phase(phase_callback, 'imports'):
        import pilk
        import vosk

    vosk.SetLogLevel(-1)
    if not _valid_silk(raw, seconds):
        return ''
    with tempfile.TemporaryDirectory(prefix='decode-', dir=scratch) as directory:
        os.chmod(directory, 0o700)
        source = Path(directory) / 'voice.silk'
        pcm = Path(directory) / 'voice.pcm'
        with _phase(phase_callback, 'decode'):
            source.write_bytes(raw)
            pilk.decode(str(source), str(pcm), pcm_rate=16000)
        size = pcm.stat().st_size
        if not 0 < size <= seconds * 16000 * 2 or size % 2:
            return ''
        with _phase(phase_callback, 'model'):
            model = vosk.Model(str(model_path))
        with _phase(phase_callback, 'recognition'):
            recognizer = vosk.KaldiRecognizer(model, 16000)
            parts = []
            with pcm.open('rb') as handle:
                while chunk := handle.read(8000):
                    if recognizer.AcceptWaveform(chunk):
                        parts.append(json.loads(recognizer.Result()).get('text', ''))
            parts.append(json.loads(recognizer.FinalResult()).get('text', ''))
            return ' '.join(part for part in parts if isinstance(part, str)).strip()[:2000]


def _memory_watchdog(stop):
    while not stop.wait(.1):
        try:
            rss=int(next(line.split()[1] for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('VmRSS:')))
        except (OSError,ValueError,StopIteration):
            os._exit(2)
        if rss>MAX_RSS_KIB:os._exit(2)


def _worker():
    # Native decoders/model loading must not exhaust the resident chat service.
    import resource
    # Model loading maps more virtual space than its measured resident memory.
    resource.setrlimit(resource.RLIMIT_AS, (2048 * 1024**2, 2048 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (12, 12))
    resource.setrlimit(resource.RLIMIT_FSIZE, (60 * 16000 * 2, 60 * 16000 * 2))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    stop=threading.Event()
    threading.Thread(target=_memory_watchdog,args=(stop,),daemon=True).start()
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        phases = {}
        def record(name, duration):
            phases[name] = duration
            (Path(sys.argv[3]) / 'phases.json').write_text(json.dumps(phases), encoding='ascii')
        text = _recognize(raw, sys.argv[1], _bounded(sys.argv[2], 60, 60), sys.argv[3], record)
        sys.stdout.buffer.write(json.dumps({'text': text}, ensure_ascii=False).encode('utf-8'))
    finally:stop.set()


if __name__ == '__main__':
    try:
        _worker()
    except Exception:
        sys.exit(1)
