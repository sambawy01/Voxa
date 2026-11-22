"""Voxa for AI agents: a REST API (/v1/...) and an MCP server (/mcp) that make speech,
podcasts and audiobooks on Voxa's server.

The website's voices run in the visitor's browser; an agent such as Claude calls from its
own servers, so here Voxa's server makes the audio, with two engines:
  - Supertonic 3 (Supertone, OpenRAIL-M): 31 languages, ten voices, the int8 files the site
    already serves (static/SUPERTONIC_DIR), on onnxruntime; about 4x faster than real time
    per CPU core.
  - Kokoro-82M (hexgrad, Apache-2.0): the Studio catalogue, through server.synth.

Access is by API key. Keys are never stored, only their SHA-256: VOXA_API_KEYS is a comma
list of name:sha256hex. Each key has a daily character allowance (VOXA_API_DAILY_CHARS).
Long work runs as a job (podcast or audiobook) on one worker thread; any line of a finished
job can be rewritten or revoiced and only that line is made again. Finished audio is served
from signed links (an HMAC of the job id), so an agent can hand the user a plain URL.

Everything an agent can do is one function of `Api`; REST routes and MCP tools are thin
wrappers over the same functions, so the two never drift apart.
"""
import hashlib
import hmac
import io
import json
import logging
import os
import queue
import re
import secrets
import threading
import time
import uuid
import wave

log = logging.getLogger('voxa.agentapi')

SR = 24000
MAX_SPEECH_CHARS = 3000          # /v1/speech answers synchronously
MAX_JOB_CHARS = 200_000          # one podcast or audiobook
MAX_LINE_CHARS = 2000
JOB_TTL = 7 * 24 * 3600
MCP_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


# --- Keys and daily allowance ---------------------------------------------------------------

class Keys:
    """API keys by SHA-256 (never the key itself) and each key's characters used today (UTC)."""

    def __init__(self, spec, data_dir, daily):
        self.by_hash = {}
        for item in (spec or '').split(','):
            item = item.strip()
            if not item:
                continue
            name, _, digest = item.partition(':')
            if not re.fullmatch(r'[0-9a-f]{64}', digest or ''):
                raise ValueError(f'VOXA_API_KEYS entry {name!r} needs name:sha256hex')
            self.by_hash[digest] = name
        self.daily = daily
        self.path = os.path.join(data_dir, 'usage.json')
        self.lock = threading.Lock()

    @staticmethod
    def digest(key):
        return hashlib.sha256(key.encode()).hexdigest()

    def check(self, key):
        if not key:
            return None
        d = self.digest(key)
        for h, name in self.by_hash.items():
            if hmac.compare_digest(h, d):
                return name
        return None

    def _load(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def used(self, name):
        day = time.strftime('%Y-%m-%d', time.gmtime())
        return self._load().get(day, {}).get(name, 0)

    def charge(self, name, n):
        """Count n characters against today's allowance; returns a refund callable."""
        if n <= 0:
            return lambda: None
        day = time.strftime('%Y-%m-%d', time.gmtime())
        with self.lock:
            data = self._load()
            today = data.get(day, {})
            if self.daily and today.get(name, 0) + n > self.daily:
                left = max(0, self.daily - today.get(name, 0))
                raise ApiError(429, f'Daily allowance reached: {left:,} of {self.daily:,} characters left today '
                                    '(resets at midnight UTC).')
            today[name] = today.get(name, 0) + n
            data = {day: today}          # only today is kept
            self._save(data)

        def refund():
            with self.lock:
                data = self._load()
                if day in data and name in data[day]:
                    data[day][name] = max(0, data[day][name] - n)
                    self._save(data)
        return refund

    def _save(self, data):
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        os.replace(tmp, self.path)


# --- Audio helpers --------------------------------------------------------------------------

def resample(x, src, dst):
    """Band-limited resampling: a windowed-sinc low-pass, then linear interpolation."""
    import numpy as np
    if src == dst:
        return x.astype(np.float32)
    if dst < src:
        cutoff = 0.97 * dst / src / 2          # in cycles per input sample
        n = np.arange(-64, 65)
        h = np.sinc(2 * cutoff * n) * np.blackman(len(n))
        h /= h.sum()
        x = np.convolve(x, h, mode='same')
    t = np.arange(int(len(x) * dst / src)) * (src / dst)
    return np.interp(t, np.arange(len(x)), x).astype(np.float32)


def wav_bytes(pcm):
    import numpy as np
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(pcm, -1, 1) * 32767).astype('<i2').tobytes())
    return buf.getvalue()


def read_wav(path):
    """16-bit mono WAV (a path or a file object) as float32."""
    import numpy as np
    with wave.open(path, 'rb') as w:
        return np.frombuffer(w.readframes(w.getnframes()), '<i2').astype(np.float32) / 32767


def mp3_bytes(pcm, title=None):
    """MP3 with lameenc when it is installed, else None (the API then offers WAV)."""
    try:
        import lameenc
    except ImportError:
        return None
    import numpy as np
    enc = lameenc.Encoder()
    enc.set_bit_rate(128)
    enc.set_in_sample_rate(SR)
    enc.set_channels(1)
    enc.set_quality(2)
    data = enc.encode((np.clip(pcm, -1, 1) * 32767).astype('<i2').tobytes()) + enc.flush()
    return bytes(data)


# --- Engines --------------------------------------------------------------------------------

class Supertonic:
    """Supertonic 3 on onnxruntime, from the int8 files the site serves (see web/supertonic-worker.js)."""
    STEPS, SPEED = 6, 1.05

    def __init__(self, folder):
        self.folder = folder
        self.sessions = None
        self.lock = threading.Lock()

    def _load(self):
        import numpy as np
        import onnxruntime as ort
        f = lambda n: os.path.join(self.folder, n)
        with open(f('tts.json'), encoding='utf-8') as fh:
            self.cfg = json.load(fh)
        with open(f('unicode_indexer.json'), encoding='utf-8') as fh:
            self.indexer = json.load(fh)
        with open(f('voices.json'), encoding='utf-8') as fh:
            self.vindex = json.load(fh)
        self.vdata = np.fromfile(f('voices.bin'), dtype='<f4')
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
        self.sessions = {m: ort.InferenceSession(f(m + '.onnx'), so, providers=['CPUExecutionProvider'])
                         for m in ('duration_predictor', 'text_encoder', 'vector_estimator', 'vocoder')}

    def _style(self, name):
        import numpy as np
        v = self.vindex['voices'][name]
        ttl = self.vdata[v['offset']:v['offset'] + v['ttl']].reshape(self.vindex['ttl_dims'])
        dp = self.vdata[v['offset'] + v['ttl']:v['offset'] + v['ttl'] + v['dp']].reshape(self.vindex['dp_dims'])
        return ttl.astype(np.float32), dp.astype(np.float32)

    @staticmethod
    def pieces(text, lang):
        mx = 120 if lang in ('ja', 'ko') else 280
        sentences = re.findall(r'[^.!?。！？؟।]+[.!?。！？؟।]*["\'”’)\]]*\s*', ' '.join(text.split())) or [text]
        out, buf = [], ''
        for s in sentences:
            while len(s) > mx:
                cut = max(s.rfind(', ', 0, mx), s.rfind('، ', 0, mx))
                if cut < mx // 3:
                    cut = s.rfind(' ', 0, mx)
                if cut < mx // 3:
                    cut = mx
                if buf.strip():
                    out.append(buf.strip())
                buf = ''
                out.append(s[:cut + 1].strip())
                s = s[cut + 1:]
            if len(buf) + len(s) > mx and buf.strip():
                out.append(buf.strip())
                buf = s
            else:
                buf += s
        if buf.strip():
            out.append(buf.strip())
        return out

    def _prepare(self, text, lang):
        import unicodedata
        t = unicodedata.normalize('NFKD', text)
        t = re.sub('[\U0001F300-\U0001FAFF☀-➿]+', '', t)
        t = re.sub('[–‑—]', '-', t)
        t = re.sub('[“”„«»]', '"', t)
        t = re.sub('[‘’´`]', "'", t)
        t = re.sub(r'[\[\]|/#\\♥☆♡©]', ' ', t)
        t = ' '.join(t.split())
        if not re.search(r'[.!?;:,\'")\]}…。」』】〉》›»؟،۔।]$', t):
            t += '.'
        return f'<{lang}>{t}</{lang}>'

    def synth(self, text, style_name, lang, seed=4242):
        import numpy as np
        with self.lock:
            if self.sessions is None:
                self._load()
            ttl, dp = self._style(style_name)
            rng = np.random.default_rng(seed)
            rate = self.cfg['ae']['sample_rate']
            chunk = self.cfg['ae']['base_chunk_size'] * self.cfg['ttl']['chunk_compress_factor']
            dim = self.cfg['ttl']['latent_dim'] * self.cfg['ttl']['chunk_compress_factor']
            gap = np.zeros(int(SR * (0.15 if lang in ('ja', 'ko') else 0.22)), np.float32)
            out = []
            for i, piece in enumerate(self.pieces(text, lang)):
                p = self._prepare(piece, lang)
                ids = np.array([[self.indexer[ord(c)] if ord(c) < len(self.indexer) else -1 for c in p]], np.int64)
                mask = np.ones((1, 1, ids.shape[1]), np.float32)
                dur = self.sessions['duration_predictor'].run(None, {'text_ids': ids, 'style_dp': dp, 'text_mask': mask})[0][0] / self.SPEED
                emb = self.sessions['text_encoder'].run(None, {'text_ids': ids, 'style_ttl': ttl, 'text_mask': mask})[0]
                n = max(1, -(-int(dur * rate) // chunk))
                xt = rng.standard_normal((1, dim, n)).astype(np.float32)
                lm = np.ones((1, 1, n), np.float32)
                for s in range(self.STEPS):
                    xt = self.sessions['vector_estimator'].run(None, {
                        'noisy_latent': xt, 'text_emb': emb, 'style_ttl': ttl, 'latent_mask': lm, 'text_mask': mask,
                        'current_step': np.array([s], np.float32), 'total_step': np.array([self.STEPS], np.float32)})[0]
                wav = self.sessions['vocoder'].run(None, {'latent': xt})[0][0][:int(rate * dur)]
                if i:
                    out.append(gap)
                out.append(resample(wav, rate, SR))
            return np.concatenate(out) if out else np.zeros(0, np.float32)


class Voices:
    """The voices an agent can use: Supertonic 3 (31 languages) and Kokoro (the Studio catalogue)."""

    def __init__(self, server):
        self.server = server
        self.supertonic = Supertonic(os.path.join(server.STATIC_DIR, server.SUPERTONIC_DIR))
        self.kokoro_lock = threading.Lock()
        labels = server.ALL_LANG_LABELS
        self.by_id = {}
        for lang, vs in (server.LITE or {}).get('voices', {}).items():
            for v in vs:
                if v['engine'] == 'supertonic':
                    self.by_id[v['id']] = {'id': v['id'], 'name': v['name'], 'gender': v['gender'], 'language': lang,
                                           'language_name': labels.get(lang, lang), 'engine': 'supertonic',
                                           'source': v['source'], 'code': v['code']}
        for v in server.CATALOGUE:
            self.by_id[v['id']] = {'id': v['id'], 'name': v['name'], 'gender': v['gender'], 'language': v['lang'],
                                   'language_name': labels.get(v['lang'], v['lang']), 'engine': 'kokoro',
                                   'accent': v.get('accent'), 'description': v.get('description')}

    def languages(self):
        langs = {}
        for v in self.by_id.values():
            langs.setdefault(v['language'], {'code': v['language'], 'name': v['language_name'], 'voices': 0})['voices'] += 1
        return sorted(langs.values(), key=lambda x: (x['code'] not in ('a', 'b'), x['name']))

    def list(self, language=None, engine=None, gender=None):
        out = [v for v in self.by_id.values()
               if (not language or v['language'] == language) and (not engine or v['engine'] == engine)
               and (not gender or v['gender'] == gender)]
        return [{k: v[k] for k in ('id', 'name', 'gender', 'language', 'language_name', 'engine', 'accent') if v.get(k)}
                for v in out]

    def get(self, voice_id):
        v = self.by_id.get(voice_id)
        if not v:
            raise ApiError(400, f'Unknown voice {voice_id!r}. Use list_voices (GET /v1/voices) to see the voices.')
        return v

    def synth(self, voice_id, text, seed=4242):
        import numpy as np
        v = self.get(voice_id)
        if v['engine'] == 'supertonic':
            return self.supertonic.synth(text, v['source'], v['code'], seed)
        with self.kokoro_lock:
            parts = [read_wav(io.BytesIO(w)) for w in self.server.synth(text, voice_id, 1.0, v['language'])]
        return np.concatenate(parts) if parts else np.zeros(0, np.float32)


# --- Scripts --------------------------------------------------------------------------------

def parse_script(raw):
    """A two-or-more-voice script: '# Title', '## Part', then 'Name: what they say' turns.
    Bold, bullets and stage directions on their own line are dropped (as web/podcast.js)."""
    title, chapters, cur = '', [], {'title': '', 'lines': []}
    names = []
    for line in raw.replace('\r', '').split('\n'):
        line = re.sub(r'^\s*(?:[-*•>]\s+|\d+[.)]\s+)', '', line).replace('**', '').replace('__', '').strip()
        if not line or re.fullmatch(r'[\[(*][^\])*]*[\])*]', line):
            continue
        h = re.match(r'^(#{1,6})\s+(.+)$', line)
        if h:
            if len(h.group(1)) == 1 and not title:
                title = h.group(2).strip()
            else:
                if cur['lines']:
                    chapters.append(cur)
                cur = {'title': h.group(2).strip(), 'lines': []}
            continue
        m = re.match(r'^([^:\n]{1,40}?)\s*:\s*(.+)$', line)
        if m and len(m.group(1).split()) <= 3 and not m.group(1).lower().startswith('http'):
            speaker = m.group(1).strip()
            if speaker.lower() not in (n.lower() for n in names):
                names.append(speaker)
            speaker = next(n for n in names if n.lower() == speaker.lower())
            cur['lines'].append({'speaker': speaker, 'text': ' '.join(m.group(2).split())})
        elif cur['lines']:
            cur['lines'][-1]['text'] += ' ' + ' '.join(line.split())
    if cur['lines']:
        chapters.append(cur)
    for i, c in enumerate(chapters):
        c['title'] = c['title'] or (title or 'Episode' if len(chapters) == 1 else f'Part {i + 1}')
    if not names:
        raise ApiError(400, 'No turns found. Write each line as "Name: what they say".')
    return title or 'Voxa podcast', names, chapters


def chapters_from_text(text):
    """Chapters of plain text, at lines like 'Chapter 3', 'CHAPTER III', 'Part One' or markdown headings."""
    pat = re.compile(r'^\s*(?:#{1,3}\s+.+|(?:chapter|part|book)\s+[\w\dIVXLC]+\b.*)$', re.I | re.M)
    marks = list(pat.finditer(text))
    if not marks:
        return [{'title': 'Chapter 1', 'text': text.strip()}]
    out = []
    if text[:marks[0].start()].strip():
        out.append({'title': 'Opening', 'text': text[:marks[0].start()].strip()})
    for i, m in enumerate(marks):
        body = text[m.end():marks[i + 1].start() if i + 1 < len(marks) else len(text)].strip()
        if body:
            out.append({'title': m.group(0).strip().lstrip('#').strip(), 'text': body})
    return out


def split_paragraphs(text, mx=MAX_LINE_CHARS):
    paras = [' '.join(p.split()) for p in re.split(r'\n\s*\n', text) if p.strip()]
    out = []
    for p in paras:
        while len(p) > mx:
            cut = p.rfind('. ', 0, mx)
            cut = cut + 1 if cut > mx // 3 else (p.rfind(' ', 0, mx) if p.rfind(' ', 0, mx) > 0 else mx)
            out.append(p[:cut].strip())
            p = p[cut:].strip()
        if p:
            out.append(p)
    return out


# --- Jobs -----------------------------------------------------------------------------------

class Job:
    def __init__(self, kind, owner, title, lines, chapters, pause, d=None):
        d = d or {}
        self.id = d.get('id') or uuid.uuid4().hex[:16]
        self.kind, self.owner, self.title = kind, owner, title
        self.lines = lines            # [{index, chapter, speaker, voice, text, done}]
        self.chapters = chapters      # titles, in order
        self.pause = pause            # seconds between turns (podcasts) or paragraphs (audiobooks)
        self.status = d.get('status', 'queued')
        self.error = d.get('error')
        self.created = d.get('created', time.time())
        self.finished = d.get('finished')
        self.duration = d.get('duration')
        self.cancelled = False

    def progress(self):
        return round(sum(1 for l in self.lines if l['done']) / max(1, len(self.lines)), 3)

    def chars(self, only_pending=False):
        return sum(len(l['text']) for l in self.lines if not (only_pending and l['done']))

    def to_dict(self):
        return {'id': self.id, 'kind': self.kind, 'owner': self.owner, 'title': self.title, 'lines': self.lines,
                'chapters': self.chapters, 'pause': self.pause, 'status': self.status, 'error': self.error,
                'created': self.created, 'finished': self.finished, 'duration': self.duration}


class Api:
    def __init__(self, server, data_dir=None, keys=None, voices=None, secret=None, daily=None, public_url=None):
        self.data_dir = data_dir or os.path.join(server.DATA_DIR, 'agent')
        os.makedirs(os.path.join(self.data_dir, 'jobs'), exist_ok=True)
        self.keys = keys or Keys(os.environ.get('VOXA_API_KEYS', ''), self.data_dir,
                                 int(os.environ.get('VOXA_API_DAILY_CHARS', '300000')) if daily is None else daily)
        self.voices = voices or Voices(server)
        self.public_url = (public_url or os.environ.get('VOXA_PUBLIC_URL', '')).rstrip('/')
        self.secret = (secret or os.environ.get('VOXA_API_SECRET') or self._secret()).encode()
        self.jobs, self.lock, self.q = {}, threading.Lock(), queue.Queue()
        self._restore()
        threading.Thread(target=self._worker, name='voxa-agent', daemon=True).start()

    @property
    def enabled(self):
        return bool(self.keys.by_hash)

    # -- storage
    def _secret(self):
        path = os.path.join(self.data_dir, 'secret')
        try:
            with open(path, encoding='utf-8') as f:
                return f.read().strip()
        except OSError:
            s = secrets.token_hex(32)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(s)
            return s

    def _dir(self, job_id):
        return os.path.join(self.data_dir, 'jobs', job_id)

    def _save(self, job):
        os.makedirs(self._dir(job.id), exist_ok=True)
        tmp = os.path.join(self._dir(job.id), 'job.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(job.to_dict(), f, ensure_ascii=False)
        os.replace(tmp, os.path.join(self._dir(job.id), 'job.json'))

    def _restore(self):
        root = os.path.join(self.data_dir, 'jobs')
        for jid in os.listdir(root):
            try:
                with open(os.path.join(root, jid, 'job.json'), encoding='utf-8') as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            job = Job(d['kind'], d['owner'], d['title'], d['lines'], d['chapters'], d['pause'], d)
            if job.status in ('queued', 'running'):
                job.status = 'queued'
                self.q.put(job.id)
            self.jobs[job.id] = job

    def _purge(self):
        import shutil
        cutoff = time.time() - JOB_TTL
        for jid, job in list(self.jobs.items()):
            if job.created < cutoff and job.status not in ('queued', 'running'):
                self.jobs.pop(jid, None)
                shutil.rmtree(self._dir(jid), ignore_errors=True)

    # -- worker
    def _worker(self):
        while True:
            jid = self.q.get()
            job = self.jobs.get(jid)
            if not job or job.cancelled or job.status == 'cancelled':
                continue
            job.status = 'running'
            self._save(job)
            try:
                for line in job.lines:
                    if job.cancelled:
                        break
                    if line['done']:
                        continue
                    pcm = self.voices.synth(line['voice'], line['text'], seed=4242 + line['index'])
                    with open(os.path.join(self._dir(job.id), f'line-{line["index"]:04d}.wav'), 'wb') as f:
                        f.write(wav_bytes(pcm))
                    line['done'] = True
                    self._save(job)
                if job.cancelled:
                    job.status = 'cancelled'
                else:
                    self._render(job)
                    job.status, job.error = 'done', None
            except ApiError as e:
                job.status, job.error = 'error', e.message
            except Exception:
                log.exception('agent job %s failed', job.id)
                job.status, job.error = 'error', 'Speech generation failed on the server. Try again, or edit the line.'
            job.finished = time.time()
            self._save(job)

    def _render(self, job):
        import numpy as np
        out, marks, t = [], [], 0
        prev = None
        for line in job.lines:
            pcm = read_wav(os.path.join(self._dir(job.id), f'line-{line["index"]:04d}.wav'))
            if prev is not None:
                pause = job.pause * (2.5 if line['chapter'] != prev['chapter'] else
                                     (1.0 if line.get('speaker') != prev.get('speaker') else 0.6))
                out.append(np.zeros(int(SR * pause), np.float32))
                t += int(SR * pause)
            if prev is None or line['chapter'] != prev['chapter']:
                marks.append({'title': job.chapters[line['chapter']], 'start': round(t / SR, 2)})
            out.append(pcm)
            t += len(pcm)
            prev = line
        audio = np.concatenate(out) if out else np.zeros(0, np.float32)
        peak = float(np.abs(audio).max()) if len(audio) else 0
        if peak > 0.98:
            audio = audio * (0.98 / peak)
        with open(os.path.join(self._dir(job.id), 'audio.wav'), 'wb') as f:
            f.write(wav_bytes(audio))
        mp3 = mp3_bytes(audio, job.title)
        if mp3:
            with open(os.path.join(self._dir(job.id), 'audio.mp3'), 'wb') as f:
                f.write(mp3)
        job.duration = round(len(audio) / SR, 2)
        job.marks = marks
        with open(os.path.join(self._dir(job.id), 'chapters.json'), 'w', encoding='utf-8') as f:
            json.dump(marks, f, ensure_ascii=False)

    # -- links
    def token(self, job_id):
        return hmac.new(self.secret, job_id.encode(), hashlib.sha256).hexdigest()[:32]

    def link(self, job_id, fmt, base):
        base = self.public_url or base
        return f'{base}/v1/jobs/{job_id}/audio.{fmt}?token={self.token(job_id)}'

    # -- what agents can do (REST and MCP share these)
    def _job(self, owner, job_id):
        job = self.jobs.get(job_id or '')
        if not job or job.owner != owner:
            raise ApiError(404, f'No job {job_id!r} for this key.')
        return job

    def job_view(self, job, base, with_lines=False):
        out = {'id': job.id, 'kind': job.kind, 'title': job.title, 'status': job.status, 'progress': job.progress(),
               'lines': len(job.lines), 'characters': job.chars(), 'created': int(job.created)}
        if job.error:
            out['error'] = job.error
        if job.status == 'done':
            out['duration_seconds'] = job.duration
            out['audio'] = {'wav': self.link(job.id, 'wav', base)}
            if os.path.exists(os.path.join(self._dir(job.id), 'audio.mp3')):
                out['audio']['mp3'] = self.link(job.id, 'mp3', base)
            try:
                with open(os.path.join(self._dir(job.id), 'chapters.json'), encoding='utf-8') as f:
                    out['chapters'] = json.load(f)
            except OSError:
                pass
        if with_lines:
            out['script'] = [{'line': l['index'], 'chapter': job.chapters[l['chapter']], 'speaker': l.get('speaker'),
                              'voice': l['voice'], 'text': l['text']} for l in job.lines]
        return out

    def _submit(self, owner, job, base):
        n = job.chars()
        if n > MAX_JOB_CHARS:
            raise ApiError(400, f'That is {n:,} characters; the most in one job is {MAX_JOB_CHARS:,}.')
        self.keys.charge(owner, n)
        with self.lock:
            self._purge()
            self.jobs[job.id] = job
        self._save(job)
        self.q.put(job.id)
        return self.job_view(job, base)

    def create_podcast(self, owner, base, title=None, script=None, lines=None, voices=None, pause_ms=350):
        voices = voices or {}
        if script:
            stitle, names, chapters = parse_script(script)
        elif lines:
            names, chapters = [], [{'title': 'Episode', 'lines': []}]
            for l in lines:
                if not l.get('speaker') or not l.get('text'):
                    raise ApiError(400, 'Each line needs "speaker" and "text".')
                ch = l.get('chapter')
                if ch and ch != chapters[-1]['title']:
                    if chapters[-1]['lines']:
                        chapters.append({'title': ch, 'lines': []})
                    else:
                        chapters[-1]['title'] = ch
                chapters[-1]['lines'].append({'speaker': l['speaker'], 'text': ' '.join(str(l['text']).split())})
                if l['speaker'] not in names:
                    names.append(l['speaker'])
            stitle = None
        else:
            raise ApiError(400, 'Give a "script" ("Name: line" turns) or "lines" ([{speaker, text}]).')
        missing = [n for n in names if n not in voices]
        if missing:
            raise ApiError(400, f'Choose a voice for each speaker: {", ".join(missing)} '
                                '(e.g. "voices": {"' + names[0] + '": "lina-a"}). See list_voices.')
        for n in names:
            self.voices.get(voices[n])
        flat = []
        for ci, c in enumerate(chapters):
            for l in c['lines']:
                if len(l['text']) > MAX_LINE_CHARS:
                    raise ApiError(400, f'A line by {l["speaker"]} is over {MAX_LINE_CHARS} characters; split it.')
                flat.append({'index': len(flat), 'chapter': ci, 'speaker': l['speaker'], 'voice': voices[l['speaker']],
                             'text': l['text'], 'done': False})
        job = Job('podcast', owner, title or stitle or 'Voxa podcast', flat, [c['title'] for c in chapters],
                  max(0.0, min(3.0, (pause_ms or 350) / 1000)))
        return self._submit(owner, job, base)

    def create_audiobook(self, owner, base, voice=None, title=None, text=None, chapters=None, pause_ms=500):
        if not voice:
            raise ApiError(400, 'Choose a "voice" (see list_voices).')
        self.voices.get(voice)
        if chapters:
            chs = [{'title': c.get('title') or f'Chapter {i + 1}', 'text': c.get('text', '')} for i, c in enumerate(chapters)]
        elif text:
            chs = chapters_from_text(text)
        else:
            raise ApiError(400, 'Give the book as "text" (chapters found for you) or "chapters" ([{title, text}]).')
        flat = []
        for ci, c in enumerate(chs):
            for p in split_paragraphs(c['text']):
                flat.append({'index': len(flat), 'chapter': ci, 'speaker': None, 'voice': voice, 'text': p, 'done': False})
        if not flat:
            raise ApiError(400, 'The book has no text to read.')
        job = Job('audiobook', owner, title or 'Voxa audiobook', flat, [c['title'] for c in chs],
                  max(0.0, min(3.0, (pause_ms or 500) / 1000)))
        return self._submit(owner, job, base)

    def get_job(self, owner, base, job_id, script=False):
        return self.job_view(self._job(owner, job_id), base, with_lines=script)

    def list_jobs(self, owner, base):
        jobs = sorted((j for j in self.jobs.values() if j.owner == owner), key=lambda j: -j.created)
        return {'jobs': [self.job_view(j, base) for j in jobs[:50]], 'characters_used_today': self.keys.used(owner),
                'daily_allowance': self.keys.daily or None}

    def edit_line(self, owner, base, job_id, line, text=None, voice=None):
        job = self._job(owner, job_id)
        if job.status in ('queued', 'running'):
            raise ApiError(409, 'The job is still being made; edit it when it is done.')
        try:
            l = job.lines[int(line)]
        except (ValueError, IndexError, TypeError):
            raise ApiError(400, f'No line {line!r}; lines are 0 to {len(job.lines) - 1} (see get_job with script).')
        if text is None and voice is None:
            raise ApiError(400, 'Give new "text", a new "voice", or both.')
        if voice is not None:
            self.voices.get(voice)
        new_text = ' '.join(str(text).split()) if text is not None else l['text']
        if len(new_text) > MAX_LINE_CHARS or not new_text:
            raise ApiError(400, f'A line needs 1 to {MAX_LINE_CHARS} characters.')
        self.keys.charge(owner, len(new_text))
        l['text'], l['voice'], l['done'] = new_text, voice or l['voice'], False
        job.status, job.error, job.cancelled = 'queued', None, False
        self._save(job)
        self.q.put(job.id)
        return self.job_view(job, base)

    def cancel_job(self, owner, base, job_id):
        job = self._job(owner, job_id)
        if job.status in ('queued', 'running'):
            job.cancelled = True
            job.status = 'cancelled'
            self._save(job)
        return self.job_view(job, base)

    def speech(self, owner, voice, text):
        if not text or len(text) > MAX_SPEECH_CHARS:
            raise ApiError(400, f'"text" needs 1 to {MAX_SPEECH_CHARS} characters (use create_audiobook for more).')
        refund = self.keys.charge(owner, len(text))
        try:
            return wav_bytes(self.voices.synth(voice, text))
        except Exception:
            refund()
            raise

    def speech_job(self, owner, base, voice, text, title=None):
        """make_speech for MCP: a one-voice job, so the agent gets a link instead of audio bytes."""
        if not text or len(text) > MAX_SPEECH_CHARS:
            raise ApiError(400, f'"text" needs 1 to {MAX_SPEECH_CHARS} characters (use create_audiobook for more).')
        self.voices.get(voice)
        flat = [{'index': i, 'chapter': 0, 'speaker': None, 'voice': voice, 'text': p, 'done': False}
                for i, p in enumerate(split_paragraphs(text))]
        return self._submit(owner, Job('speech', owner, title or 'Voxa speech', flat, ['Speech'], 0.4), base)

    def audio(self, job_id, fmt, token):
        job = self.jobs.get(job_id or '')
        if not job or not hmac.compare_digest(self.token(job.id), token or ''):
            raise ApiError(404, 'Not found.')
        if job.status != 'done':
            raise ApiError(409, f'The audio is not ready (status: {job.status}).')
        path = os.path.join(self._dir(job.id), f'audio.{fmt}')
        if not os.path.exists(path):
            raise ApiError(404, f'No {fmt} for this job.')
        with open(path, 'rb') as f:
            return f.read(), re.sub(r'[^\w\- ]+', '', job.title)[:60].strip() or 'voxa'

    # -- REST ------------------------------------------------------------------------------------
    def docs(self, base):
        """GET /v1/docs: what an agent can do here (no key needed to read it)."""
        return {'name': 'Voxa API', 'mcp': f'{base}/mcp', 'auth': 'Authorization: Bearer <key> (MCP also accepts ?key=<key>)',
                'endpoints': {
                    'GET /v1/languages': 'languages and how many voices each has',
                    'GET /v1/voices?language=&engine=&gender=': 'voices to choose from',
                    'POST /v1/speech {text, voice}': 'up to 3,000 characters, answered with a WAV',
                    'POST /v1/podcasts {script | lines, voices, title?, pause_ms?}': 'a podcast job',
                    'POST /v1/audiobooks {voice, text | chapters, title?, pause_ms?}': 'an audiobook job',
                    'GET /v1/jobs': 'your recent jobs and characters used today',
                    'GET /v1/jobs/{id}?script=1': 'status, audio links, chapter marks, and the numbered lines',
                    'PATCH /v1/jobs/{id}/lines/{n} {text?, voice?}': 'remake one line, then rebuild the audio',
                    'DELETE /v1/jobs/{id}': 'stop a job',
                    'GET /v1/jobs/{id}/audio.mp3|wav?token=': 'the finished audio (signed link, no key needed)'},
                'tools': [t['name'] for t in self.TOOLS], 'instructions': self.INSTRUCTIONS}

    def rest(self, method, path, query, body, key, base):
        """Returns (status, body, content_type, headers)."""
        if method == 'GET' and path == '/v1/docs':
            return 200, self.docs(base), None, None
        m = re.fullmatch(r'/v1/jobs/([0-9a-f]{16})/audio\.(wav|mp3)', path)
        if m and method == 'GET':
            data, name = self.audio(m.group(1), m.group(2), (query.get('token') or [''])[0])
            ctype = 'audio/mpeg' if m.group(2) == 'mp3' else 'audio/wav'
            return 200, data, ctype, {'Content-Disposition': f'attachment; filename="{name}.{m.group(2)}"'}
        owner = self.keys.check(key)
        if not owner:
            raise ApiError(401, 'An API key is needed: "Authorization: Bearer <key>".')
        q1 = lambda k: (query.get(k) or [None])[0]
        if method == 'GET' and path == '/v1/languages':
            return 200, {'languages': self.voices.languages()}, None, None
        if method == 'GET' and path == '/v1/voices':
            return 200, {'voices': self.voices.list(q1('language'), q1('engine'), q1('gender'))}, None, None
        if method == 'POST' and path == '/v1/speech':
            wav = self.speech(owner, body.get('voice'), body.get('text'))
            return 200, wav, 'audio/wav', {'Content-Disposition': 'attachment; filename="voxa.wav"'}
        if method == 'POST' and path == '/v1/podcasts':
            return 202, self.create_podcast(owner, base, body.get('title'), body.get('script'), body.get('lines'),
                                            body.get('voices'), body.get('pause_ms', 350)), None, None
        if method == 'POST' and path == '/v1/audiobooks':
            return 202, self.create_audiobook(owner, base, body.get('voice'), body.get('title'), body.get('text'),
                                              body.get('chapters'), body.get('pause_ms', 500)), None, None
        if method == 'GET' and path == '/v1/jobs':
            return 200, self.list_jobs(owner, base), None, None
        m = re.fullmatch(r'/v1/jobs/([0-9a-f]{16})', path)
        if m and method == 'GET':
            return 200, self.get_job(owner, base, m.group(1), script=q1('script') in ('1', 'true')), None, None
        if m and method == 'DELETE':
            return 200, self.cancel_job(owner, base, m.group(1)), None, None
        m = re.fullmatch(r'/v1/jobs/([0-9a-f]{16})/lines/(\d+)', path)
        if m and method in ('PATCH', 'POST'):
            return 202, self.edit_line(owner, base, m.group(1), m.group(2), body.get('text'), body.get('voice')), None, None
        raise ApiError(404, 'Not found. See /v1/docs.')

    # -- MCP (Streamable HTTP, JSON responses) -----------------------------------------------------
    TOOLS = [
        {'name': 'list_languages', 'description': 'The languages Voxa can speak, with how many voices each has.',
         'inputSchema': {'type': 'object', 'properties': {}}},
        {'name': 'list_voices', 'description': 'Voices to choose from. Filter by language code (from list_languages, e.g. "a" for US English, "ar" Arabic, "f" French), engine ("supertonic" or "kokoro") or gender. Use a voice id in the other tools.',
         'inputSchema': {'type': 'object', 'properties': {
             'language': {'type': 'string'}, 'engine': {'type': 'string', 'enum': ['supertonic', 'kokoro']},
             'gender': {'type': 'string', 'enum': ['female', 'male']}}}},
        {'name': 'make_speech', 'description': 'Read a short text (up to 3,000 characters) aloud with one voice. Returns a job; when it is done, its audio links are ready to share.',
         'inputSchema': {'type': 'object', 'required': ['text', 'voice'], 'properties': {
             'text': {'type': 'string'}, 'voice': {'type': 'string', 'description': 'A voice id from list_voices'},
             'title': {'type': 'string'}}}},
        {'name': 'create_podcast', 'description': 'Make a podcast episode from a multi-voice script. Give "script" as text ("# Title", "## Part" headings, and one turn per line as "Name: what they say") or "lines" as a list. "voices" maps each speaker name to a voice id. Returns a job; poll get_job until status is "done" for MP3/WAV links with chapter marks.',
         'inputSchema': {'type': 'object', 'required': ['voices'], 'properties': {
             'title': {'type': 'string'},
             'script': {'type': 'string', 'description': '"Name: line" turns, optional "#"/"##" headings'},
             'lines': {'type': 'array', 'items': {'type': 'object', 'required': ['speaker', 'text'], 'properties': {
                 'speaker': {'type': 'string'}, 'text': {'type': 'string'}, 'chapter': {'type': 'string'}}}},
             'voices': {'type': 'object', 'additionalProperties': {'type': 'string'},
                        'description': 'Speaker name to voice id, e.g. {"Lina": "lina-a", "Adam": "adam-a"}'},
             'pause_ms': {'type': 'integer', 'minimum': 0, 'maximum': 3000, 'description': 'Pause between turns (default 350)'}}}},
        {'name': 'create_audiobook', 'description': 'Read a book or long document aloud with one voice. Give "text" (chapters are found at "Chapter 1", "Part Two" or markdown headings) or "chapters" as [{title, text}]. Returns a job; poll get_job for MP3/WAV links with chapter marks.',
         'inputSchema': {'type': 'object', 'required': ['voice'], 'properties': {
             'voice': {'type': 'string'}, 'title': {'type': 'string'}, 'text': {'type': 'string'},
             'chapters': {'type': 'array', 'items': {'type': 'object', 'properties': {
                 'title': {'type': 'string'}, 'text': {'type': 'string'}}}},
             'pause_ms': {'type': 'integer', 'minimum': 0, 'maximum': 3000}}}},
        {'name': 'get_job', 'description': 'Status of a job: progress, and when done the MP3/WAV links, duration and chapter marks. With script=true, every line with its number, speaker, voice and text (for edit_line).',
         'inputSchema': {'type': 'object', 'required': ['job_id'], 'properties': {
             'job_id': {'type': 'string'}, 'script': {'type': 'boolean'}}}},
        {'name': 'list_jobs', 'description': 'Recent jobs for this key, and the characters used today.',
         'inputSchema': {'type': 'object', 'properties': {}}},
        {'name': 'edit_line', 'description': 'Change one line of a finished job (new wording, a different voice, or both). Only that line is made again, then the episode is rebuilt.',
         'inputSchema': {'type': 'object', 'required': ['job_id', 'line'], 'properties': {
             'job_id': {'type': 'string'}, 'line': {'type': 'integer'}, 'text': {'type': 'string'}, 'voice': {'type': 'string'}}}},
        {'name': 'cancel_job', 'description': 'Stop a job that is still being made.',
         'inputSchema': {'type': 'object', 'required': ['job_id'], 'properties': {'job_id': {'type': 'string'}}}},
    ]
    INSTRUCTIONS = ('Voxa makes speech, podcasts and audiobooks. Pick voices with list_voices (filter by language), '
                    'then create_podcast or create_audiobook; they return a job id at once. Poll get_job every few '
                    'seconds until status is "done", then give the user the mp3 link. To fix a line, read the script '
                    'with get_job(script=true) and call edit_line. Voices are synthetic; tell listeners they are AI.')

    def _call(self, owner, base, name, a):
        if name == 'list_languages':
            return {'languages': self.voices.languages()}
        if name == 'list_voices':
            return {'voices': self.voices.list(a.get('language'), a.get('engine'), a.get('gender'))}
        if name == 'make_speech':
            return self.speech_job(owner, base, a.get('voice'), a.get('text'), a.get('title'))
        if name == 'create_podcast':
            return self.create_podcast(owner, base, a.get('title'), a.get('script'), a.get('lines'), a.get('voices'),
                                       a.get('pause_ms', 350))
        if name == 'create_audiobook':
            return self.create_audiobook(owner, base, a.get('voice'), a.get('title'), a.get('text'), a.get('chapters'),
                                         a.get('pause_ms', 500))
        if name == 'get_job':
            return self.get_job(owner, base, a.get('job_id'), bool(a.get('script')))
        if name == 'list_jobs':
            return self.list_jobs(owner, base)
        if name == 'edit_line':
            return self.edit_line(owner, base, a.get('job_id'), a.get('line'), a.get('text'), a.get('voice'))
        if name == 'cancel_job':
            return self.cancel_job(owner, base, a.get('job_id'))
        raise ApiError(400, f'Unknown tool {name!r}.')

    def mcp(self, msg, key, base):
        """One JSON-RPC message in; the response dict out (None for a notification)."""
        if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0':
            return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid request'}}
        mid, method, params = msg.get('id'), msg.get('method'), msg.get('params') or {}
        if mid is None:
            return None  # notifications (e.g. notifications/initialized) need no answer
        ok = lambda result: {'jsonrpc': '2.0', 'id': mid, 'result': result}
        if method == 'initialize':
            want = params.get('protocolVersion')
            return ok({'protocolVersion': want if want in MCP_VERSIONS else MCP_VERSIONS[0],
                       'capabilities': {'tools': {'listChanged': False}},
                       'serverInfo': {'name': 'voxa', 'title': 'Voxa', 'version': '1.0.0'},
                       'instructions': self.INSTRUCTIONS})
        if method == 'ping':
            return ok({})
        if method == 'tools/list':
            return ok({'tools': self.TOOLS})
        if method == 'tools/call':
            owner = self.keys.check(key)
            if not owner:
                return ok({'isError': True, 'content': [{'type': 'text', 'text': 'Voxa needs an API key: '
                           'connect with "Authorization: Bearer <key>" or add ?key=<key> to the server URL.'}]})
            try:
                result = self._call(owner, base, params.get('name'), params.get('arguments') or {})
                return ok({'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}],
                           'structuredContent': result})
            except ApiError as e:
                return ok({'isError': True, 'content': [{'type': 'text', 'text': e.message}]})
        return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': f'Method not found: {method}'}}
