"""Voxa — server-side text to speech on Kokoro-82M.

Why a server: the browser build of Kokoro is English-only (its bundled phonemizer
ships just ["en"]) and stalls on phones. The Python runtime handles every language
the model was trained for.

Memory is the design constraint. Measured on this project:
    import torch                ~230-404 MB
    one KPipeline               ~1,257 MB   (the 82M model + voice cache + G2P)
    a SECOND KPipeline          +651 MB     <-- this OOM-killed a 2 GB machine
    a second pipeline sharing
      the first one's KModel    +0 MB       <-- so we do exactly that
    speed=0.01, two words       peak 4.6 GB <-- OOM-killed the 4 GB machine

So: load the weights ONCE, hand the same KModel to a per-language KPipeline, keep
speed inside a sane range, and run exactly ONE generation at a time.

Generation runs as a background job. A long script on a shared CPU can take minutes,
which is longer than a phone browser or the edge proxy will hold a silent connection
open — the request dropped and Safari reported "Load failed". The page now submits a
job, polls its progress, then fetches the finished audio.
"""
import collections
import datetime
import hashlib
import http.cookies
import io
import itertools
import json
import logging
import math
import os
import queue
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('voxa')

PORT = int(os.environ.get('PORT', '8080'))
MAX_TEXT = 4000
MAX_BODY = 64 * 1024
# Audio length scales with 1/speed, and so does the model's working memory for a chunk.
SPEED_MIN, SPEED_MAX = 0.5, 2.0
# Bound the cache by bytes, not entries: one 4,000-character script is ~15 MB of WAV.
MAX_CACHE_BYTES = int(os.environ.get('VOXA_CACHE_MB', '256')) * 1024 * 1024
# Jobs waiting or running before new ones are refused with 503.
MAX_PENDING = 16
JOB_TTL = 30 * 60
# How long the synchronous /api/tts endpoint waits for its job.
SYNC_TIMEOUT = 300
# Free tier: characters a visitor may generate per UTC day. 0 turns the limit off.
# Cached replays are free; only new generation counts.
FREE_DAILY_CHARS = int(os.environ.get('VOXA_FREE_DAILY_CHARS', '4000'))
# Backstop per IP address, so clearing cookies cannot buy unlimited use. Generous because
# phones on one mobile carrier often share a single address.
IP_DAILY_FACTOR = 5
# Where usage counts are stored. Mount a Fly volume at /data to keep them across restarts.
DATA_DIR = os.environ.get('VOXA_DATA_DIR') or ('/data' if os.path.isdir('/data') else '.')
COOKIE = 'voxa_id'

LANG_LABELS = {
    'a': 'English (US)',
    'b': 'English (UK)',
    'e': 'Spanish',
    'f': 'French',
    'h': 'Hindi',
    'i': 'Italian',
    'j': 'Japanese',
    'p': 'Portuguese (BR)',
    'z': 'Chinese (Mandarin)',
}

# The full Kokoro v1.0 voice catalogue (54 voices), taken from the HF repo's voices/ listing.
# Hardcoded because KPipeline.voices only reports the voices already WARM in this process —
# reporting from it gave users a single choice ("1 voices for English"), which was wrong.
# The first voice of each language is its default.
VOICES = {
    'a': ['af_heart', 'af_bella', 'af_nicole', 'af_aoede', 'af_kore', 'af_sarah',
          'af_alloy', 'af_nova', 'af_sky', 'af_jessica', 'af_river',
          'am_fenrir', 'am_michael', 'am_puck', 'am_adam', 'am_echo', 'am_eric',
          'am_liam', 'am_onyx', 'am_santa'],
    'b': ['bf_emma', 'bf_isabella', 'bf_alice', 'bf_lily',
          'bm_fable', 'bm_george', 'bm_lewis', 'bm_daniel'],
    'e': ['ef_dora', 'em_alex', 'em_santa'],
    'f': ['ff_siwis'],
    'h': ['hf_alpha', 'hf_beta', 'hm_omega', 'hm_psi'],
    'i': ['if_sara', 'im_nicola'],
    'j': ['jf_alpha', 'jf_gongitsune', 'jf_tebukuro', 'jf_nezumi', 'jm_kumo'],
    'p': ['pf_dora', 'pm_alex', 'pm_santa'],
    'z': ['zf_xiaobei', 'zf_xiaoni', 'zf_xiaoxiao', 'zf_xiaoyi',
          'zm_yunjian', 'zm_yunxi', 'zm_yunxia', 'zm_yunyang'],
}

_shared_model = None
_pipelines = {}
_model_lock = threading.Lock()
_pipe_lock = threading.Lock()


class BadRequest(ValueError):
    """A client error whose message is safe to show to the user."""


def get_shared_model():
    """Load Kokoro's weights exactly once for the whole process."""
    global _shared_model
    with _model_lock:
        if _shared_model is None:
            from kokoro import KModel

            log.info('loading Kokoro KModel (shared across languages)')
            _shared_model = KModel(repo_id='hexgrad/Kokoro-82M').to('cpu').eval()
            log.info('KModel ready')
        return _shared_model


def get_pipeline(lang_code):
    """A per-language pipeline sharing the one model. Costs only its phonemizer."""
    if lang_code not in LANG_LABELS:
        raise BadRequest(f'unsupported language code: {lang_code}')

    with _pipe_lock:
        if lang_code not in _pipelines:
            from kokoro import KPipeline

            model = get_shared_model()
            log.info('creating pipeline for %s (shared model)', lang_code)
            _pipelines[lang_code] = KPipeline(
                lang_code=lang_code, repo_id='hexgrad/Kokoro-82M', model=model
            )
            log.info('pipeline %s ready', lang_code)
        return _pipelines[lang_code]


def synth(text, voice, speed, lang_code, on_progress=None):
    """Yield one WAV (bytes) per sentence chunk, reporting the share of text consumed."""
    import numpy as np
    import soundfile as sf

    pipe = get_pipeline(lang_code)
    consumed = 0
    for graphemes, _, audio in pipe(text, voice=voice, speed=speed):
        if audio is None:
            continue
        arr = audio.detach().cpu().numpy() if hasattr(audio, 'detach') else np.asarray(audio)
        buf = io.BytesIO()
        sf.write(buf, arr, 24000, format='WAV', subtype='PCM_16')
        consumed += len(graphemes or '')
        if on_progress:
            on_progress(min(consumed / max(len(text), 1), 0.99))
        yield buf.getvalue()


def join_wavs(wavs):
    """Concatenate PCM-16 WAVs into one, so multi-sentence text is not truncated."""
    if not wavs:
        return b''
    if len(wavs) == 1:
        return wavs[0]
    import wave

    frames, params = [], None
    for blob in wavs:
        with wave.open(io.BytesIO(blob), 'rb') as w:
            p = w.getparams()
            if params is None:
                params = p
            elif (p.nchannels, p.sampwidth, p.framerate) != (
                params.nchannels, params.sampwidth, params.framerate
            ):
                raise ValueError('chunk format mismatch; cannot join')
            frames.append(w.readframes(w.getnframes()))

    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setnchannels(params.nchannels)
        w.setsampwidth(params.sampwidth)
        w.setframerate(params.framerate)
        for f in frames:
            w.writeframes(f)
    return out.getvalue()


def cache_key(text, voice, speed, lang_code):
    h = hashlib.sha256()
    h.update(text.encode('utf-8'))
    h.update(b'|')
    h.update(f'{voice}|{speed}|{lang_code}'.encode())
    return h.hexdigest()


def parse_request(body):
    """Validate a TTS request body. Returns (text, lang, voice, speed) or raises BadRequest."""
    if not isinstance(body, dict):
        raise BadRequest('request body must be a JSON object')

    text = body.get('text')
    if not isinstance(text, str):
        raise BadRequest('text must be a string')
    text = text.strip()
    if not text:
        raise BadRequest('text is empty')
    if len(text) > MAX_TEXT:
        raise BadRequest(f'text longer than {MAX_TEXT} characters')

    lang = body.get('lang') or 'a'
    if lang not in LANG_LABELS:
        raise BadRequest(f'unsupported language code: {lang!r}; see /api/languages')

    voice = body.get('voice') or VOICES[lang][0]
    if voice not in VOICES[lang]:
        raise BadRequest(f'voice {voice!r} is not available for {LANG_LABELS[lang]}; '
                         f'choose one of: {", ".join(VOICES[lang])}')

    speed = body.get('speed', 1.0)
    if speed is None:
        speed = 1.0
    if isinstance(speed, bool) or not isinstance(speed, (int, float, str)):
        raise BadRequest('speed must be a number')
    try:
        speed = float(speed)
    except ValueError:
        raise BadRequest('speed must be a number')
    if not math.isfinite(speed) or not SPEED_MIN <= speed <= SPEED_MAX:
        raise BadRequest(f'speed must be between {SPEED_MIN} and {SPEED_MAX}')

    return text, lang, voice, speed


class AudioCache:
    """LRU of finished WAVs, bounded by total bytes."""

    def __init__(self, max_bytes):
        self.max_bytes = max_bytes
        self._items = collections.OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            audio = self._items.get(key)
            if audio is not None:
                self._items.move_to_end(key)
            return audio

    def put(self, key, audio):
        if len(audio) > self.max_bytes:
            return
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= len(old)
            self._items[key] = audio
            self._bytes += len(audio)
            while self._bytes > self.max_bytes:
                _, evicted = self._items.popitem(last=False)
                self._bytes -= len(evicted)

    def __len__(self):
        with self._lock:
            return len(self._items)

    @property
    def nbytes(self):
        with self._lock:
            return self._bytes


_cache = AudioCache(MAX_CACHE_BYTES)


class QuotaExceeded(Exception):
    def __init__(self, status, requested):
        self.status = status
        self.requested = requested
        left = status['remaining']
        hours = max(1, round((status['resets_at'] - time.time()) / 3600))
        super().__init__(
            f'Free daily limit reached: {left:,} of {status["limit"]:,} characters left today '
            f'and this script is {requested:,}. It resets at midnight UTC '
            f'(in about {hours} hour{"" if hours == 1 else "s"}).'
        )


class Quota:
    """Characters generated per visitor per UTC day, in SQLite.

    A visitor is their cookie id, or their IP when there is no cookie (scripts). Every
    request also counts against its IP at IP_DAILY_FACTOR times the limit.
    """

    def __init__(self, path, limit):
        self.limit = limit
        self.ip_limit = limit * IP_DAILY_FACTOR
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute('CREATE TABLE IF NOT EXISTS usage ('
                         'day TEXT, subject TEXT, chars INTEGER NOT NULL, '
                         'PRIMARY KEY (day, subject))')
        self._db.commit()

    @property
    def enabled(self):
        return self.limit > 0

    @staticmethod
    def _today():
        now = datetime.datetime.now(datetime.timezone.utc)
        midnight = (now + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return now.date().isoformat(), midnight.timestamp()

    @staticmethod
    def _subjects(visitor, ip):
        return (f'v:{visitor}' if visitor else f'a:{ip}'), f'ip:{ip}'

    def _used(self, day, subject):
        row = self._db.execute('SELECT chars FROM usage WHERE day = ? AND subject = ?',
                               (day, subject)).fetchone()
        return row[0] if row else 0

    def _status(self, day, resets_at, visitor_key, ip_key):
        remaining = max(0, min(self.limit - self._used(day, visitor_key),
                               self.ip_limit - self._used(day, ip_key)))
        return {'limit': self.limit, 'used': self.limit - remaining,
                'remaining': remaining, 'resets_at': int(resets_at)}

    def status(self, visitor, ip):
        if not self.enabled:
            return None
        day, resets_at = self._today()
        with self._lock:
            return self._status(day, resets_at, *self._subjects(visitor, ip))

    def charge(self, visitor, ip, n):
        """Take n characters or raise QuotaExceeded. Returns a refund callable."""
        if not self.enabled:
            return lambda: None
        day, resets_at = self._today()
        keys = self._subjects(visitor, ip)
        with self._lock:
            status = self._status(day, resets_at, *keys)
            if n > status['remaining']:
                raise QuotaExceeded(status, n)
            self._add(day, keys, n)
            self._db.execute('DELETE FROM usage WHERE day < ?', (day,))
            self._db.commit()

        def refund():
            with self._lock:
                for subject in keys:
                    self._db.execute('UPDATE usage SET chars = MAX(0, chars - ?) '
                                     'WHERE day = ? AND subject = ?', (n, day, subject))
                self._db.commit()
        return refund

    def _add(self, day, keys, n):
        for subject in keys:
            self._db.execute(
                'INSERT INTO usage (day, subject, chars) VALUES (?, ?, ?) '
                'ON CONFLICT (day, subject) DO UPDATE SET chars = chars + excluded.chars',
                (day, subject, n))


def open_quota():
    path = os.path.join(DATA_DIR, 'voxa.sqlite3')
    try:
        return Quota(path, FREE_DAILY_CHARS)
    except sqlite3.Error as e:
        log.warning('cannot open %s (%s); daily limits will reset on restart', path, e)
        return Quota(':memory:', FREE_DAILY_CHARS)


_quota = None  # opened in main(), so importing this module touches no files


class Job:
    def __init__(self, seq, key, text, lang, voice, speed):
        self.id = uuid.uuid4().hex
        self.seq = seq
        self.key = key
        self.text, self.lang, self.voice, self.speed = text, lang, voice, speed
        self.status = 'queued'
        self.progress = 0.0
        self.error = None
        self.from_cache = False
        self.created = time.time()
        self.finished = None
        self.done = threading.Event()
        self.refund = None


class JobQueue:
    """One worker thread runs every generation, so model memory never stacks up."""

    def __init__(self, generate):
        self._generate = generate
        self._jobs = {}
        self._by_key = {}
        self._queue = queue.Queue()
        self._seq = itertools.count()
        self._lock = threading.Lock()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name='voxa-worker', daemon=True)
        self._thread.start()

    def submit(self, text, lang, voice, speed, charge=None):
        """Queue a generation. `charge(n)` is called only when new audio must be generated
        (not for a cache hit or a duplicate of a job in flight) and returns a refund callable,
        used if the job fails. It may raise QuotaExceeded."""
        key = cache_key(text, voice, speed, lang)
        with self._lock:
            self._purge()
            existing = self._by_key.get(key)
            if existing is not None and existing.status in ('queued', 'running'):
                return existing  # a double-tap joins the job already in flight
            cached = _cache.get(key) is not None
            if not cached and self._pending() >= MAX_PENDING:
                raise Busy()
            refund = charge(len(text)) if charge and not cached else None
            job = Job(next(self._seq), key, text, lang, voice, speed)
            job.refund = refund
            self._jobs[job.id] = job
            if cached:
                job.status, job.progress, job.from_cache = 'done', 1.0, True
                job.finished = time.time()
                job.done.set()
                return job
            self._by_key[key] = job
        self._queue.put(job)
        return job

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def ahead_of(self, job):
        with self._lock:
            return sum(1 for j in self._jobs.values()
                       if j.status in ('queued', 'running') and j.seq < job.seq)

    def _pending(self):
        return sum(1 for j in self._jobs.values() if j.status in ('queued', 'running'))

    def _purge(self):
        cutoff = time.time() - JOB_TTL
        for jid in [jid for jid, j in self._jobs.items() if j.finished and j.finished < cutoff]:
            job = self._jobs.pop(jid)
            if self._by_key.get(job.key) is job:
                del self._by_key[job.key]

    def _run(self):
        while True:
            job = self._queue.get()
            job.status = 'running'
            try:
                audio = _cache.get(job.key)
                if audio is None:
                    audio = self._generate(job)
                    if not audio:
                        raise RuntimeError('no audio produced')
                    _cache.put(job.key, audio)
                job.progress = 1.0
                job.status = 'done'
            except BadRequest as e:
                job.status, job.error = 'error', str(e)
            except Exception:
                log.exception('tts job %s failed (lang=%s voice=%s chars=%d)',
                              job.id, job.lang, job.voice, len(job.text))
                job.status = 'error'
                job.error = 'Speech generation failed on the server. Please try again.'
            finally:
                if job.status == 'error' and job.refund:
                    try:
                        job.refund()  # a failed generation does not use up free characters
                    except Exception:
                        log.exception('refund for job %s failed', job.id)
                job.finished = time.time()
                job.done.set()


class Busy(Exception):
    pass


def generate(job):
    def on_progress(p):
        job.progress = p

    return join_wavs(list(synth(job.text, job.voice, job.speed, job.lang, on_progress)))


_jobs = JobQueue(generate)


def job_json(job):
    out = {
        'id': job.id,
        'status': job.status,
        'progress': round(job.progress, 3),
        'cache': 'hit' if job.from_cache else 'miss',
    }
    if job.status == 'queued':
        out['ahead'] = _jobs.ahead_of(job)
    if job.status == 'done':
        out['audio_url'] = f'/api/audio/{job.key}'
    if job.error:
        out['error'] = job.error
    return out


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Voxa — text to speech</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Ccircle cx='8' cy='8' r='6' fill='none' stroke='%237aa2ff' stroke-width='2'/%3E%3Ccircle cx='8' cy='8' r='3' fill='%237aa2ff'/%3E%3C/svg%3E">
<style>
:root{--bg:#0f1115;--panel:#171b22;--panel2:#1e2430;--line:#2b3341;--ink:#e9edf4;--dim:#9aa5b5;--accent:#7aa2ff;--good:#6fe3b4;--bad:#ff8a8a}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1200px 600px at 50% -10%,#1b2333 0%,var(--bg) 60%);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;min-height:100vh}
.wrap{max-width:1020px;margin:0 auto;padding:30px 18px 70px}
.brand{display:flex;align-items:center;gap:10px}
.mark{color:var(--accent);font-size:20px}
h1{font-size:26px;margin:0;letter-spacing:.4px;font-weight:680}
.tagline{color:var(--dim);font-size:14px;margin:8px 0 24px;max-width:62ch}
.grid{display:grid;grid-template-columns:1fr 340px;gap:16px}
@media(max-width:860px){.grid{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}
label{display:block;font-size:11.5px;text-transform:uppercase;letter-spacing:.8px;color:var(--dim);margin:16px 0 7px}
label:first-child{margin-top:0}
textarea{width:100%;min-height:210px;background:var(--panel2);color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:13px;font:inherit;resize:vertical}
select{width:100%;background:var(--panel2);color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:10px 11px;font:inherit}
input[type=range]{width:100%;accent-color:var(--accent)}
.meta{display:flex;justify-content:space-between;color:var(--dim);font-size:12px;margin-top:7px;font-variant-numeric:tabular-nums}
.meta .over,.hint.over{color:var(--bad)}
.hint{color:var(--dim);font-size:12px;margin:6px 0 0;min-height:18px}
.val{color:var(--good);font-variant-numeric:tabular-nums;font-size:13px}
button.primary{width:100%;margin-top:18px;background:var(--accent);color:#08101f;border:0;border-radius:10px;padding:13px 18px;font:inherit;font-weight:680;cursor:pointer}
button.primary:disabled{opacity:.45;cursor:default}
button.ghost{width:100%;margin-top:8px;background:transparent;color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:10px;font:inherit;cursor:pointer}
button[hidden]{display:none}
.status{font-size:13px;color:var(--dim);margin:10px 0 0;min-height:18px}
.status.busy{color:var(--accent)}
.status.err{color:var(--bad)}
.bar{height:4px;background:var(--panel2);border-radius:4px;margin-top:10px;overflow:hidden}
.bar[hidden]{display:none}
.bar i{display:block;height:100%;width:0;background:var(--accent);transition:width .4s}
audio{width:100%;margin-top:12px}
audio[hidden]{display:none}
.download{display:block;margin-top:12px;text-align:center;background:var(--panel2);color:var(--ink);font-size:14px;font-weight:600;text-decoration:none;border:1px solid var(--line);border-radius:10px;padding:11px 16px;cursor:pointer}
.download:hover{border-color:var(--accent)}
.download[hidden]{display:none}
.note{margin-top:16px;color:var(--dim);font-size:13px;line-height:1.65}
.note strong{color:var(--ink)}
.chips{display:flex;flex-wrap:wrap;gap:7px;margin-top:12px}
.chip{background:var(--panel2);border:1px solid var(--line);color:var(--dim);border-radius:999px;padding:6px 11px;font-size:12px;cursor:pointer;font-family:inherit}
.chip:hover{border-color:var(--accent);color:var(--ink)}
</style></head>
<body><div class="wrap">
<div class="brand"><span class="mark">◉</span><h1>Voxa</h1></div>
<p class="tagline">Text to speech. Nine languages, real voices. Generated on a server, so it sounds the same on every device.</p>
<div class="grid">
  <section class="card">
    <label for="text">Your script</label>
    <textarea id="text" maxlength="4000" placeholder="Type or paste what you want spoken…"></textarea>
    <div class="meta"><span id="count">0 / 4000 characters</span><span id="est"></span></div>
    <p class="hint" id="quota"></p>
    <div class="chips">
      <button class="chip" data-lang="a">English</button>
      <button class="chip" data-lang="f">Français</button>
      <button class="chip" data-lang="e">Español</button>
      <button class="chip" data-lang="i">Italiano</button>
      <button class="chip" data-lang="j">日本語</button>
      <button class="chip" data-lang="z">中文</button>
      <button class="chip" data-lang="clear">Clear</button>
    </div>
  </section>
  <section class="card">
    <label for="lang">Language</label>
    <select id="lang"></select>
    <p class="hint" id="langnote"></p>
    <label for="voice">Voice</label>
    <select id="voice"></select>
    <p class="hint" id="voicenote"></p>
    <label for="speed">Pacing <span class="val" id="speedv">1.00×</span></label>
    <input type="range" id="speed" min="0.6" max="1.4" step="0.05" value="1">
    <button id="go" class="primary">Generate speech</button>
    <button id="cancel" class="ghost" hidden>Cancel</button>
    <div class="bar" id="bar" hidden><i></i></div>
    <p class="status" id="status" role="status"></p>
    <audio id="player" controls hidden></audio>
    <a id="download" class="download" download="voxa.wav" hidden>⬇ Download audio</a>
    <a id="playerlink" class="download" hidden>Open in player</a>
  </section>
</div>
<div class="card note"><strong>About the model.</strong> Kokoro-82M, 82 million parameters, Apache 2.0.
One set of weights serves every language — only the pronunciation rules change — so all nine
languages share the same memory. Repeated phrases are cached and come back instantly.</div>
</div>
<script>
const $=s=>document.querySelector(s);
const MAX=4000;
let LANG={}, VOICES={};
const langSel=$('#lang'), voiceSel=$('#voice');
Promise.all([
  fetch('/api/languages').then(r=>r.json()),
  fetch('/api/voices').then(r=>r.json())
]).then(([l,v])=>{
  LANG=l.languages||{}; VOICES=v.voices||{};
  langSel.innerHTML=Object.entries(LANG).map(([k,lab])=>`<option value="${k}">${lab}</option>`).join('');
  refreshVoices();
}).catch(()=>{ langSel.innerHTML='<option value="a">English (US)</option>'; });

function refreshVoices(){
  const k=langSel.value, list=VOICES[k]||[];
  voiceSel.innerHTML=list.length?list.map(v=>`<option value="${v}">${v}</option>`).join(''):'<option value="">default</option>';
  const n=list.length;
  $('#langnote').textContent = n ? `${n} voice${n===1?'':'s'} for ${LANG[k]}${n===1?' — the model only ships one for this language':''}.` : '';
}
langSel.addEventListener('change',refreshVoices);
$('#speed').addEventListener('input',e=>$('#speedv').textContent=(+e.target.value).toFixed(2)+'×');
function upd(){const t=$('#text').value;const c=$('#count');c.textContent=`${t.length} / ${MAX} characters`;c.className=t.length>MAX?'over':'';$('#est').textContent=t.length?`≈ ${Math.ceil(t.length/14)}s of audio`:'';}
$('#text').addEventListener('input',upd);upd();
const SAMPLES={a:'Voxa turns your words into speech. This audio was generated on a server, so it sounds the same on every device.',
f:'Bonjour. Ceci est Voxa, une voix de synthèse qui fonctionne sur un serveur.',
e:'Hola. Esto es Voxa, una voz sintética generada en un servidor.',
i:'Ciao. Questa è Voxa, una voce sintetica generata su un server.',
j:'こんにちは。これはサーバーで生成された合成音声です。',
z:'你好。这是在服务器上生成的合成语音。'};
document.querySelectorAll('.chip').forEach(c=>c.addEventListener('click',()=>{
  const k=c.dataset.lang;
  if(k==='clear'){$('#text').value='';}else{$('#text').value=SAMPLES[k]||'';langSel.value=k;refreshVoices();}
  upd();
}));

const sleep=ms=>new Promise(r=>setTimeout(r,ms));
// fetch() rejects with a TypeError ("Load failed" in Safari, "Failed to fetch" in Chrome) when
// the connection drops. Say what that means instead of echoing the browser's wording.
function friendly(e){
  if(e.name==='AbortError') return 'Cancelled.';
  if(e instanceof TypeError) return 'Lost the connection to the server. Check your connection and try again.';
  return e.message;
}
async function getJSON(url, opts){
  const r=await fetch(url, opts);
  const j=await r.json().catch(()=>({}));
  if(!r.ok){const err=new Error(j.error||('Server error ('+r.status+')'));err.status=r.status;throw err;}
  return j;
}
// Poll the job. A phone that sleeps or switches networks drops a request or two, so retry
// transient network errors rather than failing a generation the server is still running.
async function waitFor(job, signal, onTick){
  let misses=0;
  while(job.status==='queued'||job.status==='running'){
    onTick(job);
    await sleep(700);
    if(signal.aborted) throw new DOMException('aborted','AbortError');
    try{ job=await getJSON('/api/jobs/'+job.id,{signal,cache:'no-store'}); misses=0; }
    catch(e){
      if(e.name==='AbortError') throw e;
      if(e.status===404) throw new Error('The server restarted and lost this request. Please generate again.');
      if(e.status || ++misses>8) throw e;
      await sleep(1000*misses);
    }
  }
  if(job.status==='error') throw new Error(job.error||'Generation failed.');
  return job;
}

// Free tier: a daily character allowance, kept on the server. null means no limit.
let QUOTA=null;
function showQuota(q){
  if(q!==undefined) QUOTA=q;
  const el=$('#quota');
  if(!QUOTA){el.textContent='';return;}
  const left=QUOTA.remaining.toLocaleString(), lim=QUOTA.limit.toLocaleString();
  el.className=QUOTA.remaining?'hint':'hint over';
  el.textContent=QUOTA.remaining
    ? `${left} of ${lim} free characters left today. Replaying something you already made is free.`
    : `You've used today's ${lim} free characters. They reset at midnight UTC.`;
}
function loadQuota(){ fetch('/api/quota',{cache:'no-store'}).then(r=>r.json()).then(j=>showQuota(j.quota)).catch(()=>{}); }
loadQuota();

let ctrl=null;
$('#cancel').addEventListener('click',()=>{if(ctrl)ctrl.abort();});
$('#go').addEventListener('click',async()=>{
  const text=$('#text').value.trim(); const st=$('#status');
  if(!text){st.className='status';st.textContent='Type something first.';return;}
  if(text.length>MAX){st.className='status err';st.textContent=`That script is ${text.length} characters; the limit is ${MAX}.`;return;}
  const btn=$('#go'), bar=$('#bar'), fill=$('#bar i');
  btn.disabled=true; $('#cancel').hidden=false; $('#player').hidden=true; $('#download').hidden=true; $('#playerlink').hidden=true;
  ctrl=new AbortController();
  const t0=performance.now();
  st.className='status busy'; st.textContent='Sending…'; bar.hidden=false; fill.style.width='0';
  try{
    let job=await getJSON('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},
      signal:ctrl.signal,
      body:JSON.stringify({text,lang:langSel.value,voice:voiceSel.value||null,speed:+$('#speed').value})})
      .catch(e=>{ if(e.status===429) loadQuota(); throw e; });
    if(job.quota!==undefined) showQuota(job.quota);
    job=await waitFor(job, ctrl.signal, j=>{
      if(j.status==='queued'){st.textContent=j.ahead?`Waiting — ${j.ahead} request${j.ahead===1?'':'s'} ahead of yours…`:'Starting…';}
      else{const pct=Math.round(j.progress*100);st.textContent=`Generating… ${pct}%`;fill.style.width=Math.max(pct,3)+'%';}
    });
    fill.style.width='100%'; st.textContent='Downloading audio…';
    const r=await fetch(job.audio_url,{signal:ctrl.signal});
    if(!r.ok) throw new Error(r.status===404?'The server restarted and lost this audio. Please generate again.':'Server error ('+r.status+')');
    const blob=await r.blob();
    const url=URL.createObjectURL(blob);
    const p=$('#player'); p.src=url; p.hidden=false; p.play().catch(()=>{});

    // Download from the blob the browser ALREADY has. iOS Safari ignores the HTML `download`
    // attribute on blob URLs, so use the Web Share API there — that is what surfaces
    // "Save to Files".
    const a=$('#download');
    a.href=url; a.download='voxa.wav'; a.hidden=false;
    const pl=$('#playerlink');
    pl.href=job.audio_url; pl.download='voxa.wav'; pl.hidden=false;

    const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
                  (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    if (isIOS && navigator.canShare) {
      a.textContent = '⬇ Save or share audio';
      a.onclick = async (ev) => {
        try {
          const file = new File([blob], 'voxa.wav', { type: 'audio/wav' });
          if (navigator.canShare({ files: [file] })) {
            ev.preventDefault();
            await navigator.share({ files: [file], title: 'Voxa audio' });
          }
        } catch (e) { /* user cancelled — fall through to the plain link */ }
      };
    }
    st.className='status';
    st.textContent=`Done in ${((performance.now()-t0)/1000).toFixed(1)}s`+(job.cache==='hit'?' · from cache':'');
  }catch(e){
    loadQuota();  // a failed generation is refunded
    st.className=e.name==='AbortError'?'status':'status err'; st.textContent=(e.name==='AbortError'?'':'Error: ')+friendly(e);
  }finally{btn.disabled=false;$('#cancel').hidden=true;bar.hidden=true;ctrl=null;}
});
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json', extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Expose-Headers', 'X-Voxa-Cache, X-Voxa-File')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_audio(self, job):
        audio = _cache.get(job.key)
        if audio is None:
            return self._send(404, {'error': 'audio not found or expired from cache'})
        # Content-Disposition gives the browser a real filename. iOS Safari ignores the
        # HTML `download` attribute for blob URLs, so this header is what actually makes
        # "Save to Files" behave on a phone.
        return self._send(200, audio, 'audio/wav', {
            'X-Voxa-Cache': 'hit' if job.from_cache else 'miss',
            'X-Voxa-File': f'/api/audio/{job.key}',
            'Content-Disposition': f'attachment; filename="voxa-{job.key[:8]}.wav"',
        })

    def _client_ip(self):
        # Fly's proxy sets Fly-Client-IP to the real client address on every request.
        return (self.headers.get('Fly-Client-IP') or self.client_address[0]).strip()

    def _visitor(self):
        """The anonymous id from our cookie, or None (scripts, cookies blocked)."""
        try:
            jar = http.cookies.SimpleCookie(self.headers.get('Cookie') or '')
        except http.cookies.CookieError:
            return None
        morsel = jar.get(COOKIE)
        value = morsel.value if morsel else ''
        return value if len(value) == 32 and all(c in '0123456789abcdef' for c in value) else None

    def _new_visitor_cookie(self):
        secure = '; Secure' if self.headers.get('X-Forwarded-Proto') == 'https' else ''
        return (f'{COOKIE}={uuid.uuid4().hex}; Max-Age=31536000; Path=/; '
                f'HttpOnly; SameSite=Lax{secure}')

    def _submit(self):
        visitor, ip = self._visitor(), self._client_ip()
        charge = (lambda n: _quota.charge(visitor, ip, n)) if _quota else None
        job = _jobs.submit(*self._read_request(), charge=charge)
        return job, visitor, ip

    def _read_request(self):
        """Parse and validate the JSON body. Returns (text, lang, voice, speed)."""
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            raise BadRequest('bad Content-Length')
        if n > MAX_BODY:
            # Drain is pointless for a body this size; close the connection instead.
            self.close_connection = True
            raise BadRequest(f'request body larger than {MAX_BODY} bytes')
        try:
            body = json.loads(self.rfile.read(n) or b'{}')
        except (ValueError, UnicodeDecodeError) as e:
            raise BadRequest(f'bad json: {e}')
        return parse_request(body)

    def do_GET(self):
        try:
            self._get()
        except Exception:
            log.exception('GET %s failed', self.path)
            self._send(500, {'error': 'internal server error'})

    def do_POST(self):
        try:
            self._post()
        except BadRequest as e:
            self._send(400, {'error': str(e)})
        except Busy:
            self._send(503, {'error': 'The server is busy. Please try again in a minute.'},
                       extra={'Retry-After': '30'})
        except QuotaExceeded as e:
            self._send(429, {'error': str(e), 'quota': e.status},
                       extra={'Retry-After': str(max(1, int(e.status['resets_at'] - time.time())))})
        except Exception:
            log.exception('POST %s failed', self.path)
            self._send(500, {'error': 'internal server error'})

    def _get(self):
        p = urlparse(self.path).path
        if p in ('/', '/index.html'):
            extra = {} if self._visitor() else {'Set-Cookie': self._new_visitor_cookie()}
            return self._send(200, PAGE, 'text/html; charset=utf-8', extra)
        if p == '/api/quota':
            status = _quota.status(self._visitor(), self._client_ip()) if _quota else None
            return self._send(200, {'quota': status}, extra={'Cache-Control': 'no-store'})
        if p.startswith('/api/jobs/'):
            job = _jobs.get(p.rsplit('/', 1)[-1])
            if job is None:
                return self._send(404, {'error': 'job not found; the server may have restarted'})
            return self._send(200, job_json(job), extra={'Cache-Control': 'no-store'})
        # Download endpoint: lets iOS Safari fetch the file directly, which is the only way
        # its share sheet offers "Save to Files" for generated audio.
        if p.startswith('/api/audio/'):
            key = p.rsplit('/', 1)[-1]
            audio = _cache.get(key)
            if audio is None:
                return self._send(404, {'error': 'audio not found or expired from cache'})
            return self._send(200, audio, 'audio/wav', {
                'Content-Disposition': f'attachment; filename="voxa-{key[:8]}.wav"',
                'Cache-Control': 'private, max-age=3600',
            })
        if p == '/health':
            return self._send(200, {
                'ok': True,
                'model_loaded': _shared_model is not None,
                'pipelines': list(_pipelines),
                'cached': len(_cache),
                'cache_mb': round(_cache.nbytes / 1048576, 1),
            })
        if p == '/api/languages':
            return self._send(200, {'languages': LANG_LABELS})
        if p == '/api/voices':
            return self._send(200, {'voices': VOICES, 'languages': LANG_LABELS})
        return self._send(404, {'error': 'not found'})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _post(self):
        p = urlparse(self.path).path
        if p == '/api/jobs':
            job, visitor, ip = self._submit()
            out = job_json(job)
            if _quota:
                out['quota'] = _quota.status(visitor, ip)
            return self._send(200 if job.status == 'done' else 202, out)
        if p == '/api/tts':
            # Synchronous form for scripts and API callers: same queue, waits for the result.
            job, _, _ = self._submit()
            if not job.done.wait(SYNC_TIMEOUT):
                return self._send(504, {
                    'error': 'still generating; poll the job instead',
                    'job': job_json(job),
                })
            if job.status == 'error':
                return self._send(500, {'error': job.error})
            return self._send_audio(job)
        return self._send(404, {'error': 'not found'})


def main():
    global _quota
    _quota = open_quota()
    if _quota.enabled:
        log.info('free tier: %d characters per visitor per day', _quota.limit)
    _jobs.start()
    try:
        get_pipeline('a')  # warm English
    except Exception as e:
        log.warning('warmup failed: %s', e)
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    srv.daemon_threads = True
    log.info('Voxa listening on :%s', PORT)
    srv.serve_forever()


if __name__ == '__main__':
    main()
