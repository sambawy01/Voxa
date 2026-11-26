# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy>=1.24", "onnxruntime>=1.17", "lameenc>=1.4"]
# ///
"""Voxa for AI agents: a REST API (/v1/...) and an MCP server (/mcp) that make speech,
podcasts and audiobooks on Voxa's server.

Run on its own, this file is also a local MCP server for Claude Desktop and Claude Code: it
downloads Supertonic 3 from Voxa once and makes everything on the user's own computer, saving
MP3s to a folder there. See main() and docs/API.md.

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
import sys
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
        self.prepare = None           # called once before loading, e.g. to download the files

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
        if lang == 'ar':
            text = normalize_arabic(text)                 # digits as words: the voice cannot read them
        with self.lock:
            if self.sessions is None:
                if self.prepare:
                    self.prepare()
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


# --- Arabic numbers: digits read as words ----------------------------------------------------
# The Arabic voices read "1998" or "01012345678" as noise, but read the same numbers written as
# words perfectly. So digits become words before the voice sees them: cardinals that agree with the
# counted noun (ثلاث سنوات, ثلاثة كتب), years and amounts, phone numbers digit by digit in groups,
# times, percentages, decimals, dates and ranges. The browser does the same (web/arabic-numbers.js);
# tests/arabic_numbers_cases.json holds the cases both must pass.

_AR_UM = ['صفر', 'واحد', 'اثنين', 'ثلاثة', 'أربعة', 'خمسة', 'ستة', 'سبعة', 'ثمانية', 'تسعة', 'عشرة']
_AR_UF = ['صفر', 'واحدة', 'اثنتين', 'ثلاث', 'أربع', 'خمس', 'ست', 'سبع', 'ثمان', 'تسع', 'عشر']
_AR_TM = {11: 'أحد عشر', 12: 'اثني عشر', 13: 'ثلاثة عشر', 14: 'أربعة عشر', 15: 'خمسة عشر', 16: 'ستة عشر', 17: 'سبعة عشر',
          18: 'ثمانية عشر', 19: 'تسعة عشر'}
_AR_TF = {11: 'إحدى عشرة', 12: 'اثنتي عشرة', 13: 'ثلاث عشرة', 14: 'أربع عشرة', 15: 'خمس عشرة', 16: 'ست عشرة', 17: 'سبع عشرة',
          18: 'ثماني عشرة', 19: 'تسع عشرة'}
_AR_TENS = {2: 'عشرين', 3: 'ثلاثين', 4: 'أربعين', 5: 'خمسين', 6: 'ستين', 7: 'سبعين', 8: 'ثمانين', 9: 'تسعين'}
_AR_HUND = {1: 'مائة', 2: 'مائتين', 3: 'ثلاثمائة', 4: 'أربعمائة', 5: 'خمسمائة', 6: 'ستمائة', 7: 'سبعمائة', 8: 'ثمانمائة', 9: 'تسعمائة'}
_AR_SCALES = [(10 ** 9, 'مليار', 'مليارين', 'مليارات'), (10 ** 6, 'مليون', 'مليونين', 'ملايين'), (1000, 'ألف', 'ألفين', 'آلاف')]
_AR_HOUR = {1: 'الأولى', 2: 'الثانية', 3: 'الثالثة', 4: 'الرابعة', 5: 'الخامسة', 6: 'السادسة', 7: 'السابعة', 8: 'الثامنة',
            9: 'التاسعة', 10: 'العاشرة', 11: 'الحادية عشرة', 12: 'الثانية عشرة'}
# Feminine counted nouns the endings do not show (and masculine plurals that end in ات).
_AR_FEM = set('دقائق ثوان ثواني ليالي سنين دول مدن نساء أوراق رسائل أفكار قطع نقاط عيون أيدي أعين أم أخت بنت يد عين دار نار شمس أرض حرب'.split())
_AR_MASC = set('ملفات أوقات تطبيقات فيديوهات مشروبات مستندات حسابات كيلومترات سنتيمترات ملليمترات دولارات جنيهات ريالات جرامات كيلوجرامات لترات أمتار'.split())
_AR_DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬', '01234567890123456789.,')
_AR_RUN = re.compile(r'(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\+?\d{2,4}(?:[ \-]\d{2,4}){2,}|\+?\d+(?:[.:/\-]\d+)*)(\s*%)?')


def _ar_u1000(n, fem):
    parts = []
    h, r = divmod(n, 100)
    if h:
        parts.append(_AR_HUND[h])
    if r:
        if r <= 10:
            parts.append((_AR_UF if fem else _AR_UM)[r])
        elif r < 20:
            parts.append((_AR_TF if fem else _AR_TM)[r])
        else:
            t, u = divmod(r, 10)
            if u:
                unit = 'إحدى' if (u == 1 and fem) else (_AR_UF if fem else _AR_UM)[u]
                parts.append(unit + ' و' + _AR_TENS[t])
            else:
                parts.append(_AR_TENS[t])
    return ' و'.join(parts)


def _ar_cardinal(n, fem=False):
    """1998 -> ألف وتسعمائة وثمانية وتسعون. `fem`: the counted noun is feminine (ثلاث سنوات)."""
    if n == 0:
        return 'صفر'
    parts, rest = [], n
    for size, one, two, few in _AR_SCALES:
        q, rest = divmod(rest, size)
        if not q:
            continue
        if q == 1:
            parts.append(one)
        elif q == 2:
            parts.append(two)
        elif q == 200:
            parts.append('مائتي ' + one)
        else:
            parts.append(_ar_u1000(q, False) + ' ' + (few if 3 <= q % 100 <= 10 else one))
    if rest:
        parts.append(_ar_u1000(rest, fem))
    return ' و'.join(parts)


def _ar_is_fem(word):
    w = re.sub('[ً-ٰٟـ]', '', word or '')
    w = re.sub('^(?:[وفبلك]?ال|لل)', '', w)
    if w in _AR_FEM:
        return True
    if w in _AR_MASC:
        return False
    return w.endswith('ة') or w.endswith('ات')


def _ar_digits(s, sep=' '):
    return sep.join(_AR_UM[int(c)] for c in s)


def _ar_phone_groups(digits, raw):
    groups = re.findall(r'\d+', raw)
    if len(groups) >= 3 and sum(map(len, groups)) == len(digits) and not raw.startswith('+'):
        return groups
    if raw.startswith('+') and len(groups) >= 3:
        return groups
    if re.fullmatch(r'01[0125]\d{8}', digits):
        return [digits[:3], digits[3:7], digits[7:]]
    out, i = [], 0
    while i < len(digits):
        out.append(digits[i:i + 3])
        i += 3
    if len(out) > 1 and len(out[-1]) == 1:
        tail = out.pop()
        out[-1] += tail
    return out


def _ar_is_phone(run):
    digits = re.sub(r'\D', '', run)
    if len(digits) < 8:
        return False
    if run.startswith('+') or digits.startswith('0'):
        return True
    groups = re.findall(r'\d+', run)
    return bool(re.fullmatch(r'[\d ]+', run) and (len(groups) == 1 and len(digits) >= 9 or
                                                    len(groups) >= 3 and all(3 <= len(g) <= 4 for g in groups)))


def _ar_phone(run):
    digits = re.sub(r'\D', '', run)
    words = '، '.join(_ar_digits(g) for g in _ar_phone_groups(digits, run))
    return ('زائد ' if run.startswith('+') else '') + words


def _ar_time(h, m):
    hh = h % 12 or 12
    if m == 0:
        return _AR_HOUR[hh]
    if m == 15:
        return _AR_HOUR[hh] + ' وربع'
    if m == 30:
        return _AR_HOUR[hh] + ' ونصف'
    if m == 45:
        return _AR_HOUR[hh % 12 + 1] + ' إلا ربع'
    if m == 1:
        mins = 'دقيقة واحدة'
    elif m == 2:
        mins = 'دقيقتان'
    elif m <= 10:
        mins = _ar_cardinal(m, True) + ' دقائق'
    else:
        mins = _ar_cardinal(m, True) + ' دقيقة'
    return _AR_HOUR[hh] + ' و' + mins


def _ar_int(s, fem):
    if len(s) > 1 and s[0] == '0':
        return _ar_digits(s)                          # codes such as 007
    n = int(s)
    if n >= 10 ** 12:
        return '، '.join(_ar_digits(s[i:i + 3]) for i in range(0, len(s), 3))
    return _ar_cardinal(n, fem)


def _ar_compact(c, fem, hour):
    m = re.fullmatch(r'(\d{1,2}):(\d{2})', c)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h <= 24 and mi <= 59:
            return _ar_time(h, mi)
        return _ar_int(m.group(1), False) + '، ' + _ar_int(m.group(2), False)
    m = re.fullmatch(r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})|(\d{4})-(\d{1,2})-(\d{1,2})', c)
    if m:
        g = [x for x in m.groups() if x is not None]
        if m.group(4):
            g = [g[2], g[1], g[0]]                    # 2024-05-12 -> day, month, year
        return '، '.join(_ar_int(str(int(x)), False) for x in g)       # 05 is the month five, not a code
    if re.fullmatch(r'\d+(?:\.\d+){2,}', c):
        return ' نقطة '.join(_ar_int(x, False) for x in c.split('.'))
    m = re.fullmatch(r'(\d+)\.(\d+)', c)
    if m:
        frac = m.group(2)
        tail = _ar_int(frac, False) if (len(frac) <= 3 and frac[0] != '0') else _ar_digits(frac)
        return _ar_int(m.group(1), False) + ' فاصلة ' + tail
    m = re.fullmatch(r'(\d+)-(\d+)', c)
    if m:
        return _ar_int(m.group(1), fem) + ' إلى ' + _ar_int(m.group(2), fem)
    m = re.fullmatch(r'(\d+)/(\d+)', c)
    if m:
        return _ar_int(m.group(1), False) + ' على ' + _ar_int(m.group(2), False)
    if c.isdigit():
        if hour and 1 <= int(c) <= 12:
            return _AR_HOUR[int(c)]
        return _ar_int(c, fem)
    return '، '.join(_ar_int(x, fem) for x in re.findall(r'\d+', c))


def normalize_arabic(text):
    """Arabic text with its digits written as words, for the voice."""
    if not text or not re.search(r'[0-9٠-٩۰-۹]', text):
        return text
    t = text.translate(_AR_DIGITS)
    out, last = [], 0
    for m in _AR_RUN.finditer(t):
        a, b = m.start(), m.end()
        before = t[a - 1] if a else ''
        after = t[b] if b < len(t) else ''
        if re.search(r'[A-Za-z_][\-./:]?$', t[:a]) or re.match(r'[\-./:]?[A-Za-z_]', t[b:]):
            continue                                    # mp3, A4, COVID-19, 5G: not a number to read out
        run, pct = m.group(1), bool(m.group(2))
        nxt = re.match(r'\s*([ء-يٱ-ۓ]+)', t[b:])
        fem = _ar_is_fem(nxt.group(1)) if nxt else False
        prev = re.search(r'([ء-يٱ-ۓ]+)\s*$', t[:a])
        hour = bool(prev and re.sub('[ً-ٟ]', '', prev.group(1)) in ('الساعة', 'الساعه'))
        if ',' in run:                                  # 15,000: an amount, never a phone number
            words = _ar_compact(run.replace(',', ''), fem, hour)
        elif _ar_is_phone(run):
            words = _ar_phone(run)
        elif ' ' in run and not re.fullmatch(r'\d{4}-\d{1,2}-\d{1,2}', run):
            words = ' '.join(_ar_compact(x, fem, hour) for x in run.split())
        else:
            words = _ar_compact(run, fem, hour)
        if pct:
            words += ' بالمئة'
        out.append(t[last:a] + words)
        last = b
    out.append(t[last:])
    return ''.join(out)


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


# --- On the user's own device: the in-chat player (MCP Apps) ----------------------------------

MAX_DEVICE_CHARS = 60_000        # a browser tab makes this; about an hour of audio
PLAYER_URI = 'ui://voxa/player.html'
STYLES = {'steady': 0.15, 'natural': 0.3, 'expressive': 0.5}
# The best-sounding pairs, from listening tests: (woman, man) per language.
DEVICE_HOSTS = {'a': ('wren', 'calvin')}


class DeviceVoices:
    """The website's on-device voices (server.LITE): what the in-chat player can use."""

    def __init__(self, lite, labels):
        self.by_id = {}
        for lang, vs in (lite or {}).get('voices', {}).items():
            for v in vs:
                self.by_id[v['id']] = {'id': v['id'], 'name': v['name'], 'gender': v.get('gender'), 'language': lang,
                                       'language_name': labels.get(lang, lang), 'engine': v['engine'],
                                       'desktop': bool(v.get('desktop')), 'description': v.get('description', '')}

    def list(self, language=None, gender=None):
        return [{k: v[k] for k in ('id', 'name', 'gender', 'language', 'engine', 'desktop', 'description') if v.get(k)}
                for v in self.by_id.values()
                if (not language or v['language'] == language) and (not gender or v['gender'] == gender)]

    def languages(self):
        langs = {}
        for v in self.by_id.values():
            langs.setdefault(v['language'], {'code': v['language'], 'name': v['language_name'], 'voices': 0})['voices'] += 1
        return sorted(langs.values(), key=lambda x: (x['code'] not in ('a', 'b'), x['name']))

    def get(self, voice_id):
        v = self.by_id.get(voice_id)
        if not v:
            raise ApiError(400, f'Unknown on-device voice {voice_id!r}. Use list_device_voices to see them.')
        return v

    def defaults(self, language, n):
        """n voices for a language: the tested pair first, then women and men in turn; phone-friendly first."""
        pool = [v for v in self.by_id.values() if v['language'] == language]
        if not pool:
            raise ApiError(400, f'No on-device voices for language {language!r}. Use list_device_voices.')
        pool.sort(key=lambda v: v['desktop'])
        first = [self.by_id[i] for i in DEVICE_HOSTS.get(language, ()) if i in self.by_id]
        women = [v for v in pool if v['gender'] == 'female' and v not in first]
        men = [v for v in pool if v['gender'] != 'female' and v not in first]
        order = list(first)
        while women or men:
            for side in (women, men):
                if side:
                    order.append(side.pop(0))
        return [order[i % len(order)]['id'] for i in range(n)]


VIEW_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
html,body{margin:0;background:transparent;font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;color:#e7e5f2}
.card{border-radius:16px;padding:16px 18px;background:#0f0d1a;border:1px solid rgba(255,255,255,.08)}
.brand{font:800 11px system-ui;letter-spacing:.14em;text-transform:uppercase;color:#8f97b5}
h1{font:800 20px/1.2 system-ui;margin:6px 0 4px;color:#f2f4ff}
.who{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 14px}
.who span{padding:4px 10px;border-radius:999px;background:rgba(167,139,250,.14);font-size:13px}
.sub,.note{color:#a8a3bd;margin:0}
.note{font-size:12.5px;margin-top:10px}
button{font:750 14.5px system-ui;border:0;border-radius:14px;padding:12px 18px;cursor:pointer;color:#fff;
  background:linear-gradient(115deg,#5e17eb,#7c3aed 55%,#22d3ee)}
button:focus-visible{outline:2px solid #a78bfa;outline-offset:2px}
iframe{display:block;width:100%;height:440px;border:0;border-radius:16px;background:#0f0d1a;margin-bottom:10px}
.small button{padding:8px 12px;font-size:13px;background:rgba(255,255,255,.08)}
</style></head><body>
<div class="card" id="card"><div class="brand">Voxa · made on your device</div><h1 id="title">Getting the Voxa player ready…</h1>
<p class="sub" id="sub"></p><div class="who" id="who"></div>
<div id="act" hidden><button id="go" type="button">▶ Open the Voxa player</button>
<p class="note">Opens in your browser, where Voxa's voices make the audio on your device: free, nothing sent to a server.</p></div></div>
<script>
const SITE = __SITE__;
let seq = 0, frame = null, theme = null, allowed;   // allowed: the frame origins Claude reports (undefined: not reported)
const pending = new Map();
function send(msg) { window.parent.postMessage(msg, '*'); }
function request(method, params) {
  const id = ++seq;
  send({ jsonrpc: '2.0', id, method, params });
  return new Promise((ok, fail) => pending.set(id, { ok, fail }));
}
const notify = (method, params) => send({ jsonrpc: '2.0', method, params });
let lastSize = '';
function size() {
  const w = document.body.scrollWidth, h = document.body.scrollHeight;
  if (w + 'x' + h === lastSize) return;
  lastSize = w + 'x' + h;
  notify('ui/notifications/size-changed', { width: w, height: h });
}
new ResizeObserver(size).observe(document.body);
const openLink = url => request('ui/open-link', { url }).catch(() => {});

function show(data) {
  if (!data || !data.lines) return;
  const $ = id => document.getElementById(id);
  $('title').textContent = data.title || 'Voxa';
  $('sub').textContent = `${data.lines.length} lines · ${data.minutes >= 1 ? 'about ' + data.minutes + ' min' : 'under a minute'}`;
  $('who').replaceChildren(...data.speakers.map(s => Object.assign(document.createElement('span'), { textContent: `${s.name} · ${s.voice_name}` })));
  $('act').hidden = false;
  $('go').onclick = () => openLink(data.player_url);
  // Play right here when Claude lets the view frame Voxa; otherwise (or as well) the button opens it.
  const canFrame = allowed === undefined || allowed.some(o => o === SITE || (o.includes('*.') && SITE.endsWith(o.split('*')[1])));
  if (!canFrame || frame) return size();
  const hash = 'd=' + btoa(unescape(encodeURIComponent(JSON.stringify(data)))).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
  frame = document.createElement('iframe');
  frame.allow = 'autoplay; clipboard-write';
  frame.title = 'Voxa player';
  frame.src = SITE + '/player?embed=1' + (theme ? '&theme=' + theme : '') + '#' + hash;
  document.body.prepend(frame);
  document.getElementById('card').classList.add('small');
  $('go').textContent = 'Open in a browser tab ↗';
  size();
}

window.addEventListener('message', e => {
  const m = e.data;
  if (frame && e.source === frame.contentWindow) {            // from the player
    if (m && m.type === 'voxa-open' && typeof m.url === 'string' && m.url.startsWith(SITE + '/')) openLink(m.url);
    if (m && m.type === 'voxa-height' && m.height > 120) { frame.style.height = Math.min(900, m.height) + 'px'; size(); }
    return;
  }
  if (e.source !== window.parent || !m || m.jsonrpc !== '2.0') return;
  if (m.id != null && pending.has(m.id)) {
    const p = pending.get(m.id); pending.delete(m.id);
    return m.error ? p.fail(m.error) : p.ok(m.result);
  }
  if (m.method === 'ui/notifications/tool-result') {
    const r = m.params || {};
    if (r.isError) { document.getElementById('title').textContent = ((r.content || [])[0] || {}).text || 'Voxa could not open the player.'; return size(); }
    show(r.structuredContent);
  }
  if (m.id != null && m.method) send({ jsonrpc: '2.0', id: m.id, result: {} });   // e.g. ui/resource-teardown
});

request('ui/initialize', {
  protocolVersion: '2026-01-26',
  appInfo: { name: 'Voxa player', version: '1.0.0' }, clientInfo: { name: 'Voxa player', version: '1.0.0' },
  appCapabilities: { availableDisplayModes: ['inline'] }, capabilities: {},
}).then(r => {
  theme = r && r.hostContext && r.hostContext.theme;
  const fd = r && r.hostCapabilities && r.hostCapabilities.sandbox && r.hostCapabilities.sandbox.csp && r.hostCapabilities.sandbox.csp.frameDomains;
  allowed = Array.isArray(fd) ? fd : undefined;
  notify('ui/notifications/initialized', {});
  size();
}).catch(() => notify('ui/notifications/initialized', {}));
</script></body></html>
"""


def _b64(text):
    import base64
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip('=')


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
        self.files = d.get('files')   # local server: the saved audio on the user's computer
        self.cancelled = False

    def progress(self):
        return round(sum(1 for l in self.lines if l['done']) / max(1, len(self.lines)), 3)

    def chars(self, only_pending=False):
        return sum(len(l['text']) for l in self.lines if not (only_pending and l['done']))

    def to_dict(self):
        return {'id': self.id, 'kind': self.kind, 'owner': self.owner, 'title': self.title, 'lines': self.lines,
                'chapters': self.chapters, 'pause': self.pause, 'status': self.status, 'error': self.error,
                'created': self.created, 'finished': self.finished, 'duration': self.duration, 'files': self.files}


class Api:
    def __init__(self, server, data_dir=None, keys=None, voices=None, secret=None, daily=None, public_url=None,
                 out_dir=None):
        self.data_dir = data_dir or os.path.join(server.DATA_DIR, 'agent')
        os.makedirs(os.path.join(self.data_dir, 'jobs'), exist_ok=True)
        self.keys = keys or Keys(os.environ.get('VOXA_API_KEYS', ''), self.data_dir,
                                 int(os.environ.get('VOXA_API_DAILY_CHARS', '300000')) if daily is None else daily)
        self.voices = voices or Voices(server)
        self.server, self._device = server, None
        self.out_dir = out_dir        # local server: finished audio is copied here, and links are file paths
        self.public_url = (public_url or os.environ.get('VOXA_PUBLIC_URL', '')).rstrip('/')
        self.secret = (secret or os.environ.get('VOXA_API_SECRET') or self._secret()).encode()
        self.jobs, self.lock, self.q = {}, threading.Lock(), queue.Queue()
        self.save_lock = threading.Lock()   # the worker and request threads both save jobs
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
        with self.save_lock:
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
                    if self.out_dir:
                        self._keep(job)
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

    def _keep(self, job):
        """Local server: copy the finished audio to the user's folder, named after the title."""
        import shutil
        os.makedirs(self.out_dir, exist_ok=True)
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', ' ', job.title).strip().rstrip('.')[:80] or 'Voxa'
        files = {}
        for fmt in ('mp3', 'wav'):
            src = os.path.join(self._dir(job.id), f'audio.{fmt}')
            if not os.path.exists(src) or (fmt == 'wav' and 'mp3' in files):
                continue
            old = (job.files or {}).get(fmt)
            dst = old if old and os.path.dirname(old) == self.out_dir else None
            n = 1
            while dst is None:
                cand = os.path.join(self.out_dir, f'{name}{"" if n == 1 else f" ({n})"}.{fmt}')
                dst = cand if not os.path.exists(cand) else None
                n += 1
            shutil.copyfile(src, dst)
            files[fmt] = dst
        job.files = files

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
            if self.out_dir:
                out['files'] = job.files or {}
            else:
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
    # -- on the user's device (no key: nothing runs on Voxa's server)
    @property
    def device(self):
        if self._device is None:
            self._device = DeviceVoices(getattr(self.server, 'LITE', None), getattr(self.server, 'ALL_LANG_LABELS', {}))
        return self._device

    def device_play(self, base, a):
        """Check a script or text for the in-chat player and give it everything it needs."""
        script, text = a.get('script'), a.get('text')
        if script:
            title, names, chapters = parse_script(script)
        elif text:
            speaker = 'Narrator'
            chapters = [{'title': c['title'], 'lines': [{'speaker': speaker, 'text': p} for p in split_paragraphs(c['text'])]}
                        for c in chapters_from_text(text)]
            title, names = 'Voxa reading', [speaker]
            if a.get('voice'):
                a = {**a, 'voices': {speaker: a['voice']}}
        else:
            raise ApiError(400, 'Give a "script" ("Name: line" turns) or a "text" to read.')
        title = a.get('title') or title
        chosen = dict(a.get('voices') or {})
        for n, vid in chosen.items():
            self.device.get(vid)
        lang = a.get('language') or next((self.device.get(v)['language'] for v in chosen.values()), 'a')
        free = [n for n in names if n not in chosen]
        if free:
            taken = set(chosen.values())
            spare = [v for v in self.device.defaults(lang, len(names) + len(taken)) if v not in taken]
            for n, vid in zip(free, spare + self.device.defaults(lang, len(free))):
                chosen[n] = vid
        lines = [{'speaker': l['speaker'], 'text': l['text'], 'chapter': ci}
                 for ci, c in enumerate(chapters) for l in c['lines']]
        n = sum(len(l['text']) for l in lines)
        if n > MAX_DEVICE_CHARS:
            raise ApiError(400, f'That is {n:,} characters; the player makes up to {MAX_DEVICE_CHARS:,} at a time. '
                                'Split it into episodes.')
        style = (a.get('style') or 'natural').lower()
        if style not in STYLES:
            raise ApiError(400, f'"style" is one of {", ".join(STYLES)}.')
        speakers = []
        for name in names:
            v = self.device.get(chosen[name])
            speakers.append({'name': name, 'voice': v['id'], 'voice_name': v['name'], 'engine': v['engine'],
                             'gender': v['gender'], 'desktop': v['desktop']})
        pause = max(0.0, min(3.0, (a.get('pause_ms') or 350) / 1000))
        return {'title': title, 'language': lang, 'style': style, 'temperature': STYLES[style], 'pause': pause,
                'chapters': [c['title'] for c in chapters], 'speakers': speakers, 'lines': lines,
                'characters': n, 'minutes': round(n / 15 / 60, 1), 'site': base}

    PLAYER_KEYS = ('title', 'style', 'temperature', 'pause', 'chapters', 'speakers', 'lines')
    MAX_PLAYS = 5000                  # stored scripts for long player links; the oldest go first

    def player_link(self, d, base):
        """A link that opens the player with this script. Short scripts travel in the link itself (after #, so
        they never reach a server); long ones are kept here as text, 30 days, and the link carries an id."""
        data = json.dumps({k: d[k] for k in self.PLAYER_KEYS}, ensure_ascii=False, separators=(',', ':'))
        link = base + '/player#d=' + _b64(data)
        if len(link) <= 6000:
            return link
        folder = os.path.join(self.data_dir, 'plays')
        os.makedirs(folder, exist_ok=True)
        old = sorted((os.path.getmtime(os.path.join(folder, n)), n) for n in os.listdir(folder))
        for t, n in old[:max(0, len(old) - self.MAX_PLAYS + 1)] + [x for x in old if x[0] < time.time() - 30 * 86400]:
            try:
                os.remove(os.path.join(folder, n))
            except OSError:
                pass
        pid = uuid.uuid4().hex[:16]
        with open(os.path.join(folder, pid + '.json'), 'w', encoding='utf-8') as f:
            f.write(data)
        return f'{base}/player?p={pid}'

    def stored_play(self, pid):
        if not re.fullmatch(r'[0-9a-f]{16}', pid or ''):
            raise ApiError(404, 'Not found.')
        try:
            with open(os.path.join(self.data_dir, 'plays', pid + '.json'), 'rb') as f:
                return f.read()
        except OSError:
            raise ApiError(404, 'This player link has expired. Ask the AI to make it again.') from None

    def device_summary(self, d, base):
        who = ', '.join(f"{s['name']} ({s['voice_name']}{', desktop only' if s['desktop'] else ''})" for s in d['speakers'])
        link = self.player_link(d, base)
        out = (f'The Voxa player is showing "{d["title"]}" in the chat: {len(d["lines"])} lines, '
               f'{"about %s minutes" % d["minutes"] if d["minutes"] >= 1 else "under a minute"}, '
               f'voices: {who}. It makes the audio in the user\'s own browser, so nothing is sent to a server and there is '
               'no limit; the first time, the browser downloads the voices (tens of MB). The user presses Play, '
               'and can save an MP3 when it is done.')
        return out + f' If no player appears (an app without MCP Apps), give the user this link, which opens the same player on Voxa: {link}'

    def public_docs(self, base):
        return {'name': 'Voxa API', 'free': True,
                'about': "Voxa's voices run in the user's own browser or computer: free, no key, no limit.",
                'mcp': f'{base}/mcp', 'openapi': f'{base}/openapi.json', 'local_mcp': f'uv run {base}/voxa-mcp.py',
                'endpoints': {
                    'POST /v1/play {script | text, voices?, voice?, language?, style?, title?}':
                        'a link to the Voxa player, which makes and plays the audio in the browser',
                    'GET /v1/device/voices?language=&gender=': 'the on-device voices (no language: the languages)'},
                'tools': [t['name'] for t in self.DEVICE_TOOLS], 'instructions': self.DEVICE_INSTRUCTIONS}

    def docs(self, base, key=None):
        """GET /v1/docs: the free API; with a key, also the server's own (private) API."""
        if not self.keys.check(key):
            return self.public_docs(base)
        return {'name': 'Voxa API', 'free_api': self.public_docs(base), 'mcp': f'{base}/mcp', 'auth': 'Authorization: Bearer <key> (MCP also accepts ?key=<key>)',
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
                'tools': [t['name'] for t in self.DEVICE_TOOLS + self.TOOLS], 'instructions': self.INSTRUCTIONS}

    def openapi(self, base):
        """OpenAPI 3.1 for the free endpoints, e.g. for a custom GPT's actions."""
        play = next(t for t in self.DEVICE_TOOLS if t['name'] == 'play_on_device')
        return {
            'openapi': '3.1.0',
            'info': {'title': 'Voxa', 'version': '1.0.0',
                     'description': "Voxa reads podcasts and texts aloud with voices that run in the user's own browser. "
                                    'Free, no key. Call playPodcast with a script, then give the user player_url.'},
            'servers': [{'url': base}],
            'paths': {
                '/v1/play': {'post': {
                    'operationId': 'playPodcast',
                    'summary': 'Get a link to the Voxa player for a podcast script or a text',
                    'description': 'Write the script with one turn per line as "Name: what they say" (optional "# Title" and '
                                   '"## Part" headings), or give "text" to read with one voice. Returns player_url: the '
                                   "user opens it and presses Play; the audio is made in their browser. Voices are synthetic.",
                    'requestBody': {'required': True, 'content': {'application/json': {'schema': play['inputSchema']}}},
                    'responses': {'200': {'description': 'The player link', 'content': {'application/json': {'schema': {
                        'type': 'object', 'properties': {
                            'player_url': {'type': 'string'}, 'title': {'type': 'string'}, 'lines': {'type': 'integer'},
                            'minutes': {'type': 'number'},
                            'speakers': {'type': 'array', 'items': {'type': 'object', 'properties': {
                                'name': {'type': 'string'}, 'voice': {'type': 'string'}, 'voice_name': {'type': 'string'}}}}}}}}},
                                  '400': {'description': 'Something to fix, in "error"'}}}},
                '/v1/device/voices': {'get': {
                    'operationId': 'listVoices',
                    'summary': 'On-device voices for a language, or the languages when none is given',
                    'parameters': [
                        {'name': 'language', 'in': 'query', 'schema': {'type': 'string'},
                         'description': 'e.g. "a" US English, "b" British English, "ar" Arabic, "f" French, "de" German'},
                        {'name': 'gender', 'in': 'query', 'schema': {'type': 'string', 'enum': ['female', 'male']}}],
                    'responses': {'200': {'description': 'Voices or languages', 'content': {'application/json': {'schema': {'type': 'object'}}}}}}},
            }}

    def rest(self, method, path, query, body, key, base):
        """Returns (status, body, content_type, headers)."""
        if method == 'GET' and path == '/v1/docs':
            return 200, self.docs(base, key), None, None
        if method == 'GET' and path == '/v1/openapi.json':
            return 200, self.openapi(base), None, None
        if method == 'POST' and path == '/v1/play':
            d = self.device_play(base, body or {})
            return 200, {'player_url': self.player_link(d, base), 'title': d['title'], 'speakers': d['speakers'],
                         'lines': len(d['lines']), 'minutes': d['minutes'],
                         'note': "Give the user player_url: the audio is made in their browser when they press Play."}, None, None
        if method == 'GET' and path == '/v1/device/voices':
            q = lambda k: (query.get(k) or [None])[0]
            return 200, self._device_call(base, 'list_device_voices', {'language': q('language'), 'gender': q('gender')}), None, None
        m = re.fullmatch(r'/v1/play/([0-9a-f]{16})', path)
        if m and method == 'GET':
            return 200, self.stored_play(m.group(1)), 'application/json', {'Cache-Control': 'public, max-age=86400'}
        if method == 'GET' and path == '/v1/local':
            return 200, self.local_manifest(base), None, None
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
    # Free and keyless: the audio is made in the user's browser, not on Voxa's server.
    DEVICE_TOOLS = [
        {'name': 'list_device_voices', 'title': 'On-device voices',
         'description': "Voices that run in the user's own browser (for play_on_device), in about 50 languages. "
                        'Filter by language code (e.g. "a" US English, "b" British English, "ar" Arabic, "f" French, '
                        '"de" German); leave it out to get the languages. Voices marked desktop need a computer, not a phone.',
         'inputSchema': {'type': 'object', 'properties': {
             'language': {'type': 'string'}, 'gender': {'type': 'string', 'enum': ['female', 'male']}}},
         'annotations': {'readOnlyHint': True}},
        {'name': 'play_on_device', 'title': 'Play in the chat with Voxa',
         'description': "Show the Voxa player in the chat and make the audio in the user's own browser: free, private, "
                        'no API key, no limit. Give a podcast "script" (one turn per line as "Name: what they say", '
                        'optional "# Title" and "## Part" headings) or a "text" to read aloud. Voices are chosen for '
                        'each speaker unless you map names to ids from list_device_voices. The user presses Play and '
                        'can save an MP3. Up to 60,000 characters.',
         'inputSchema': {'type': 'object', 'properties': {
             'title': {'type': 'string'},
             'script': {'type': 'string', 'description': '"Name: line" turns, optional "#"/"##" headings'},
             'text': {'type': 'string', 'description': 'Plain text to read with one voice (instead of a script)'},
             'voices': {'type': 'object', 'additionalProperties': {'type': 'string'},
                        'description': 'Speaker name to on-device voice id, e.g. {"Maya": "wren", "Leo": "calvin"}'},
             'voice': {'type': 'string', 'description': 'The voice id for "text"'},
             'language': {'type': 'string', 'description': 'Language code for the default voices (default "a", US English)'},
             'style': {'type': 'string', 'enum': ['steady', 'natural', 'expressive'], 'description': 'Delivery (default natural)'},
             'pause_ms': {'type': 'integer', 'minimum': 0, 'maximum': 3000}}},
         '_meta': {'ui': {'resourceUri': PLAYER_URI}, 'ui/resourceUri': PLAYER_URI}},
    ]
    INSTRUCTIONS = ('Voxa makes speech, podcasts and audiobooks. To play something in the chat, use play_on_device: '
                    "it runs in the user's browser, free and without a key. For files to share, with a key: pick voices with list_voices (filter by language), "
                    'then create_podcast or create_audiobook; they return a job id at once. Poll get_job every few '
                    'seconds until status is "done", then give the user the mp3 link. To fix a line, read the script '
                    'with get_job(script=true) and call edit_line. Voices are synthetic; tell listeners they are AI.')

    LOCAL_TOOLS = [
        {'name': 'open_audio', 'title': 'Play a saved file',
         'description': "Open a finished job's audio file in the computer's default player.",
         'inputSchema': {'type': 'object', 'required': ['job_id'], 'properties': {'job_id': {'type': 'string'}}}},
    ]
    LOCAL_INSTRUCTIONS = ('Voxa runs on this computer. create_podcast, create_audiobook and make_speech make the audio '
                          'here and save an MP3 in the folder named in each finished job ("files"); poll get_job until '
                          'status is "done", then tell the user the file path, or call open_audio to play it. Voices: '
                          'list_voices (Supertonic 3, 31 languages). To play something in the chat instead, use '
                          'play_on_device. Voices are synthetic; tell listeners they are AI.')

    # Titles and hints for every tool (Claude uses them for permissions; the directory requires them).
    ANNOTATIONS = {
        'list_device_voices': ('On-device voices', True), 'play_on_device': ('Play in the chat with Voxa', True),
        'list_languages': ('Languages', True), 'list_voices': ('Voices', True), 'get_job': ('Job status', True),
        'list_jobs': ('Recent jobs', True), 'make_speech': ('Read a text aloud', False),
        'create_podcast': ('Make a podcast', False), 'create_audiobook': ('Make an audiobook', False),
        'edit_line': ('Remake one line', 'destructive'), 'cancel_job': ('Stop a job', 'destructive'),
        'open_audio': ('Play a saved file', False)}

    DEVICE_INSTRUCTIONS = ("Voxa reads podcasts and texts aloud in the user's own browser, free. Write the script "
                           '("Name: line" turns, optional "# Title" and "## Part" headings) and call play_on_device; '
                           'the Voxa player appears in the chat and the user presses Play. Pick voices with '
                           'list_device_voices, or leave them to Voxa. Voices are synthetic; tell listeners they are AI.')

    def open_audio(self, owner, job_id):
        import subprocess
        job = self._job(owner, job_id)
        path = next(iter((job.files or {}).values()), None)
        if job.status != 'done' or not path or not os.path.exists(path):
            raise ApiError(400, 'That job has no saved audio yet; check get_job.')
        if sys.platform == 'darwin':
            subprocess.Popen(['open', path])
        elif os.name == 'nt':
            os.startfile(path)  # noqa: S606 (a file this server wrote)
        else:
            subprocess.Popen(['xdg-open', path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {'opened': path}

    # -- for the local server: what it downloads (GET /v1/local, no key)
    def local_manifest(self, base):
        if getattr(self, '_manifest', None) is None:
            folder = os.path.join(self.server.STATIC_DIR, self.server.SUPERTONIC_DIR)
            files = {}
            for name in sorted(os.listdir(folder)):
                h = hashlib.sha256()
                with open(os.path.join(folder, name), 'rb') as f:
                    for block in iter(lambda: f.read(1 << 20), b''):
                        h.update(block)
                files[name] = {'sha256': h.hexdigest(), 'size': os.path.getsize(os.path.join(folder, name))}
            lite = self.server.LITE or {}
            self._manifest = {'version': 1, 'supertonic': {'dir': self.server.SUPERTONIC_DIR, 'files': files},
                              'languages': self.server.ALL_LANG_LABELS,
                              'lite': {'voices': lite.get('voices', {}), 'expressiveness': lite.get('expressiveness', [])}}
        return {**self._manifest, 'base': f'{base}/static/{self.server.SUPERTONIC_DIR}/'}

    def _device_call(self, base, name, a):
        if name == 'list_device_voices':
            if not a.get('language'):
                return {'languages': self.device.languages(),
                        'note': 'Call again with a language code for its voices.'}
            return {'voices': self.device.list(a.get('language'), a.get('gender'))}
        d = self.device_play(base, a)
        return d, self.device_summary(d, base)

    def player_resource(self, base):
        return {'uri': PLAYER_URI, 'name': 'Voxa player', 'mimeType': 'text/html;profile=mcp-app',
                'description': "Makes and plays speech in the user's browser with Voxa's on-device voices.",
                'text': VIEW_HTML.replace('__SITE__', json.dumps(base)),
                '_meta': {'ui': {'csp': {'frameDomains': [base], 'connectDomains': [], 'resourceDomains': []},
                                 'prefersBorder': False}}}

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
        if name == 'open_audio' and self.out_dir:
            return self.open_audio(owner, a.get('job_id'))
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
                       'capabilities': {'tools': {'listChanged': False}, 'resources': {},
                                        'extensions': {'io.modelcontextprotocol/ui': {'mimeTypes': ['text/html;profile=mcp-app']}}},
                       'serverInfo': {'name': 'voxa', 'title': 'Voxa', 'version': '1.0.0'},
                       'instructions': self.LOCAL_INSTRUCTIONS if self.out_dir else
                       self.INSTRUCTIONS if self.keys.check(key) else self.DEVICE_INSTRUCTIONS})
        if method == 'ping':
            return ok({})
        if method == 'tools/list':
            if not self.out_dir and not self.keys.check(key):
                return ok({'tools': self.DEVICE_TOOLS})     # without a key: only the free, on-device tools
            return ok({'tools': self.DEVICE_TOOLS + self.TOOLS + (self.LOCAL_TOOLS if self.out_dir else [])})
        if method == 'resources/list':
            r = self.player_resource(base)
            return ok({'resources': [{k: r[k] for k in ('uri', 'name', 'mimeType', 'description')}]})
        if method == 'resources/read':
            if params.get('uri') != PLAYER_URI:
                return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32002, 'message': 'Resource not found'}}
            r = self.player_resource(base)
            return ok({'contents': [{k: r[k] for k in ('uri', 'mimeType', 'text', '_meta')}]})
        if method == 'tools/call' and params.get('name') in ('list_device_voices', 'play_on_device'):
            try:
                result = self._device_call(base, params['name'], params.get('arguments') or {})
            except ApiError as e:
                return ok({'isError': True, 'content': [{'type': 'text', 'text': e.message}]})
            if params['name'] == 'play_on_device':
                data, summary = result
                data = {**data, 'player_url': self.player_link(data, base)}
                return ok({'content': [{'type': 'text', 'text': summary}], 'structuredContent': data})
            return ok({'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}],
                       'structuredContent': result})
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


# --- The local server: `uv run <site>/voxa-mcp.py` -------------------------------------------

DEFAULT_SITE = 'https://voxa-zpukmw.fly.dev'


class LocalKeys:
    """On the user's own computer there is no key and no allowance."""
    by_hash = {'local': 'local'}
    daily = 0

    def check(self, key):
        return 'local'

    def charge(self, name, n):
        return lambda: None

    def used(self, name):
        return 0


def _fetch(url, timeout=60):
    import urllib.request
    req = urllib.request.Request(url, headers={'User-Agent': 'voxa-mcp/1'})
    return urllib.request.urlopen(req, timeout=timeout)


def local_site(site, home):
    """What Voices and Api read from `server`, for the user's computer. The catalogue comes from
    the site (and is kept, so the server starts offline); the model files download on first use."""
    from types import SimpleNamespace
    cache = os.path.join(home, 'manifest.json')
    try:
        with _fetch(f'{site}/v1/local', timeout=20) as r:
            manifest = json.load(r)
        os.makedirs(home, exist_ok=True)
        with open(cache + '.tmp', 'w', encoding='utf-8') as f:
            json.dump(manifest, f)
        os.replace(cache + '.tmp', cache)
    except Exception as e:  # offline: use the last one
        try:
            with open(cache, encoding='utf-8') as f:
                manifest = json.load(f)
            log.warning('Voxa is not reachable (%s); using the saved voice list.', e)
        except OSError:
            raise SystemExit(f'Voxa: cannot reach {site} to fetch the voice list ({e}).') from None
    st = manifest['supertonic']
    folder = os.path.join(home, 'models', st['dir'])
    lock = threading.Lock()

    def download():
        with lock:
            for name, meta in st['files'].items():
                path = os.path.join(folder, name)
                if os.path.exists(path) and os.path.getsize(path) == meta['size']:
                    continue
                os.makedirs(folder, exist_ok=True)
                log.info('Voxa: downloading %s (%.0f MB, once)', name, meta['size'] / 1e6)
                h, tmp = hashlib.sha256(), path + '.part'
                with _fetch(manifest['base'] + name, timeout=120) as r, open(tmp, 'wb') as f:
                    for block in iter(lambda: r.read(1 << 20), b''):
                        h.update(block)
                        f.write(block)
                if h.hexdigest() != meta['sha256']:
                    os.remove(tmp)
                    raise ApiError(502, f'The download of {name} was damaged; try again.')
                os.replace(tmp, path)

    server = SimpleNamespace(STATIC_DIR=os.path.join(home, 'models'), SUPERTONIC_DIR=st['dir'],
                             ALL_LANG_LABELS=manifest['languages'], LITE=manifest['lite'], CATALOGUE=[],
                             DATA_DIR=home, synth=None)
    return server, download


def serve_stdio(api, base, stdin=None, stdout=None):
    """MCP over stdio: one JSON-RPC message per line in, one per line out."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            out = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
        else:
            try:
                out = ([r for r in (api.mcp(m, 'local', base) for m in msg) if r] or None) if isinstance(msg, list) \
                    else api.mcp(msg, 'local', base)
            except Exception:
                log.exception('MCP message failed')
                out = {'jsonrpc': '2.0', 'id': msg.get('id') if isinstance(msg, dict) else None,
                       'error': {'code': -32603, 'message': 'Internal error'}}
        if out:
            stdout.write(json.dumps(out, ensure_ascii=False) + '\n')
            stdout.flush()


def default_out_dir():
    home = os.path.expanduser('~')
    music = os.path.join(home, 'Music')
    return os.path.join(music if os.path.isdir(music) else home, 'Voxa')


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog='voxa-mcp', description='Voxa on this computer, for Claude Desktop and Claude Code (MCP over stdio).')
    p.add_argument('--out', default=os.environ.get('VOXA_OUT') or default_out_dir(), help='where finished audio is saved')
    p.add_argument('--site', default=os.environ.get('VOXA_SITE') or DEFAULT_SITE, help='the Voxa site to download from')
    p.add_argument('--home', default=os.environ.get('VOXA_HOME') or os.path.join(os.path.expanduser('~'), '.voxa'),
                   help='where models and jobs are kept')
    p.add_argument('--check', action='store_true', help='download the voices, say one sentence, and exit')
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format='%(message)s')
    site, out = a.site.rstrip('/'), os.path.abspath(os.path.expanduser(a.out))
    server, download = local_site(site, a.home)
    api = Api(server, data_dir=a.home, keys=LocalKeys(), out_dir=out)
    api.voices.supertonic.prepare = download
    if a.check:
        job = api.speech_job('local', site, 'lina-a', 'Hello! Voxa is working on this computer.', 'Voxa check')
        while api.jobs[job['id']].status in ('queued', 'running'):
            time.sleep(0.2)
        print(json.dumps(api.get_job('local', site, job['id']), indent=2))
        return
    threading.Thread(target=lambda: _quiet(download), name='voxa-download', daemon=True).start()
    log.info('Voxa MCP server ready; audio is saved to %s', out)
    serve_stdio(api, site)


def _quiet(fn):
    try:
        fn()
    except Exception as e:  # tried again on first use
        log.warning('Voxa: download failed for now (%s)', e)


def _annotate():
    for tool in Api.DEVICE_TOOLS + Api.TOOLS + Api.LOCAL_TOOLS:
        title, kind = Api.ANNOTATIONS[tool['name']]
        tool['title'] = title
        tool['annotations'] = {'title': title, 'readOnlyHint': kind is True, 'destructiveHint': kind == 'destructive',
                               'openWorldHint': False}


_annotate()


if __name__ == '__main__':
    main()
