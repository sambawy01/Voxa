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
import base64
import binascii
import collections
import datetime
import hashlib
import hmac
import html
import http.cookies
import io
import itertools
import json
import logging
import math
import os
import queue
import re
import sqlite3
import threading
import time
import urllib.request
import uuid

import billing
import podcast
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('voxa')

PORT = int(os.environ.get('PORT', '8080'))
# Where Kokoro runs: 'cpu' here; the GPU service (gpu/service.py) sets 'cuda'.
DEVICE = os.environ.get('VOXA_DEVICE', 'cpu')
# Paid users' jobs go to the GPU service when it is configured (see docs/GPU.md). Calls are
# remote, so several run at once on their own workers and never wait behind CPU jobs.
GPU_URL = os.environ.get('VOXA_GPU_URL', '').rstrip('/')
GPU_TOKEN = os.environ.get('VOXA_GPU_TOKEN', '')
GPU_WORKERS = int(os.environ.get('VOXA_GPU_WORKERS', '4'))
GPU_TIMEOUT = 600
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
# Two plans, both with an account: Voxa Lite (free) and Voxa Studio (paid). On-device voices
# are unlimited on both; these are the daily Studio-voice characters (0 would mean no limit).
# Cached replays are free; only new generation counts. Once sign-in is configured, visitors
# who are not signed in can only hear voice previews; until then they get Lite's allowance.
TIERS = {'free': 'Voxa Lite', 'paid': 'Voxa Studio'}
FREE_DAILY_CHARS = int(os.environ.get('VOXA_FREE_DAILY_CHARS', '1500'))
PAID_DAILY_CHARS = int(os.environ.get('VOXA_PAID_DAILY_CHARS', '10000'))
# Podcast scripts per tier per day (each is a paid DeepSeek call, so here 0 means none), and
# the longest episode each tier may ask for.
PODCAST_DAILY = {'guest': int(os.environ.get('VOXA_PODCAST_DAILY_GUEST', '0')),
                 'free': int(os.environ.get('VOXA_PODCAST_DAILY_FREE', '1')),
                 'paid': int(os.environ.get('VOXA_PODCAST_DAILY_PAID', '20'))}
PODCAST_MAX_MINUTES = {'free': int(os.environ.get('VOXA_PODCAST_MINUTES_FREE', '5')),
                       'paid': int(os.environ.get('VOXA_PODCAST_MINUTES_PAID', '20'))}
# Document text for a podcast script is far bigger than a speech request.
MAX_PODCAST_BODY = 1024 * 1024
# Sign-in (Supabase). SUPABASE_URL and the public anon key turn accounts on; the page uses
# them to sign people in. Tokens signed with the project's asymmetric keys are checked
# against its published JWKS; SUPABASE_JWT_SECRET covers projects on the older shared secret.
SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_ANON_KEY = os.environ.get('SUPABASE_ANON_KEY', '')
SUPABASE_JWT_SECRET = os.environ.get('SUPABASE_JWT_SECRET', '')
# Backstop per IP address, so clearing cookies cannot buy unlimited use. Generous because
# phones on one mobile carrier often share a single address.
IP_DAILY_FACTOR = 5
# Where usage counts are stored. Mount a Fly volume at /data to keep them across restarts.
DATA_DIR = os.environ.get('VOXA_DATA_DIR') or ('/data' if os.path.isdir('/data') else '.')
COOKIE = 'voxa_id'
# Who runs Voxa and how to reach them, shown on the terms, privacy and refund pages.
OPERATOR = os.environ.get('VOXA_OPERATOR', '[operator name]')
CONTACT_EMAIL = os.environ.get('VOXA_CONTACT_EMAIL', '[contact email]')
# The site's public address, for links Paddle sends people back to. Falls back to the request.
PUBLIC_URL = os.environ.get('VOXA_PUBLIC_URL', '').rstrip('/')
# Voxa Lite: the phonon-tts browser package, fetched by scripts/fetch_phonon.py into static/
# and served from here, since its worker must be same-origin. Pages that run it are served
# cross-origin isolated, which is what gives WebAssembly its threads.
HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, 'static')
WEB_DIR = os.path.join(HERE, 'web')
PHONON_VERSION = '0.4.1'
PDFJS_VERSION = '4.4.168'
LAMEJS_VERSION = '1.2.1'
STATIC_TYPES = {'.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
                '.wasm': 'application/wasm', '.bcmap': 'application/octet-stream',
                '.json': 'application/json', '.css': 'text/css; charset=utf-8'}
ISOLATION = {'Cross-Origin-Opener-Policy': 'same-origin',
             'Cross-Origin-Embedder-Policy': 'require-corp'}
# Pages and scripts served from web/, all cross-origin isolated. {{NAME}} placeholders in them
# are filled from web_values().
# Served without cross-origin isolation: Paddle's checkout runs in a frame from Paddle's own
# site, which an isolated page would refuse to embed.
PLAIN_PAGES = {'/checkout'}
WEB_PAGES = {'/': 'index.html', '/voxa.css': 'voxa.css', '/checkout': 'checkout.html', '/pricing': 'pricing.html', '/terms': 'terms.html',
             '/privacy': 'privacy.html', '/refunds': 'refunds.html', '/lite/benchmark': 'lite-benchmark.html', '/book': 'book.html', '/book.js': 'book.js',
             '/mp3-worker.js': 'mp3-worker.js', '/credits': 'credits.html',
             '/podcast': 'podcast.html', '/podcast.js': 'podcast.js', '/preview.js': 'preview.js',
             '/listen': 'listen.html', '/listen.js': 'listen.js', '/manifest.webmanifest': 'manifest.webmanifest'}
# Binary files from web/: the app icons for home screens.
WEB_FILES = {'/apple-touch-icon.png': 'icons/apple-touch-icon.png', '/icons/icon-192.png': 'icons/icon-192.png',
             '/icons/icon-512.png': 'icons/icon-512.png'}


def web_values():
    """{{NAME}} placeholders for web/ pages. The shared head, top bar and footer come first, so
    placeholders inside them are filled too."""
    def version(name):
        with open(os.path.join(WEB_DIR, name), 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]

    def partial(name):
        with open(os.path.join(WEB_DIR, 'partials', name), encoding='utf-8') as f:
            return f.read()
    return {'HEAD': partial('head.html'), 'NAV': partial('nav.html'), 'FOOTER': partial('footer.html'),
            'CSS_VERSION': version('voxa.css'), 'PHONON_VERSION': PHONON_VERSION, 'PDFJS_VERSION': PDFJS_VERSION,
            'LAMEJS_VERSION': LAMEJS_VERSION, 'BOOK_VERSION': version('book.js'),
            'PODCAST_VERSION': version('podcast.js'), 'PREVIEW_VERSION': version('preview.js'),
            'LISTEN_VERSION': version('listen.js'),
            'FREE_DAILY': f'{FREE_DAILY_CHARS:,}', 'PAID_DAILY': f'{PAID_DAILY_CHARS:,}',
            'PODCAST_MINUTES_FREE': str(PODCAST_MAX_MINUTES['free']),
            'PODCAST_MINUTES_PAID': str(PODCAST_MAX_MINUTES['paid']),
            'OPERATOR': html.escape(OPERATOR), 'CONTACT_EMAIL': html.escape(CONTACT_EMAIL),
            'PRICE_LABEL': html.escape(billing.PRICE_LABEL or 'see the price at checkout')}

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

# The voice catalogue: Voxa's own names for all 54 Kokoro v1.0 voices plus signature blends.
# Kept in voices.json so names can change without touching code. It is the source of truth
# because KPipeline.voices only reports the voices already WARM in this process — reporting
# from it gave users a single choice ("1 voices for English"), which was wrong.
CATALOGUE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'voices.json')
GRADE_LABELS = {'A': 'Best quality', 'B': 'Very good quality', 'C': 'Good quality',
                'D': 'Fair quality', 'F': 'Basic quality'}


def load_catalogue(path=CATALOGUE_PATH):
    """Read voices.json and check it. Returns the voice list, each language's default first."""
    with open(path, encoding='utf-8') as f:
        voices = json.load(f)['voices']
    seen = set()
    for v in voices:
        if v['id'] in seen:
            raise ValueError(f'duplicate voice id {v["id"]!r}')
        seen.add(v['id'])
        if v['lang'] not in LANG_LABELS:
            raise ValueError(f'voice {v["id"]!r} has unknown language {v["lang"]!r}')
        src = v['source']
        parts = [src] if isinstance(src, str) else list(src)
        if any(p[0] != v['lang'] for p in parts):
            raise ValueError(f'voice {v["id"]!r} mixes in another language')
        if not isinstance(src, str) and abs(sum(src.values()) - 1) > 1e-6:
            raise ValueError(f'blend weights of {v["id"]!r} must add up to 1')
        grade = (v.get('grade') or '')[:1]
        bits = [v['gender'].capitalize(), v['accent']]
        bits.append('Voxa signature blend' if v.get('signature') else GRADE_LABELS.get(grade))
        v['description'] = ' · '.join(b for b in bits if b)
    voices.sort(key=lambda v: not v.get('default'))
    return voices


def load_lite(path=CATALOGUE_PATH, studio_ids=()):
    """The Voxa Lite section of voices.json: Phonon voices the browser runs itself."""
    with open(path, encoding='utf-8') as f:
        lite = json.load(f).get('lite')
    if not lite:
        return None
    ids = [v['id'] for v in lite['voices']]
    if len(set(ids)) != len(ids) or set(ids) & set(studio_ids):
        raise ValueError('Lite voice ids must be unique and differ from Studio voice ids')
    for v in lite['voices']:
        if v['lang'] not in ('a', 'b'):
            raise ValueError(f'Lite voice {v["id"]!r}: Phonon 31m speaks English only')
        if not re.fullmatch(r'voices/[A-Za-z0-9_-]+\.safetensors', v['file']):
            raise ValueError(f'Lite voice {v["id"]!r} has an unexpected file path')
        v['description'] = ' · '.join([v['gender'].capitalize(), v['accent'], v['age'],
                                       'On this device'])
    if not any(e.get('default') for e in lite['expressiveness']):
        raise ValueError('one expressiveness preset must be the default')
    base = f'https://huggingface.co/{lite["voices_repo"]}/resolve/{lite["revision"]}/'
    return {
        'model': lite['model'],
        'expressiveness': lite['expressiveness'],
        'voices': {lang: [{'id': v['id'], 'name': v['name'], 'description': v['description'],
                           'gender': v['gender'], 'url': base + v['file']}
                          for v in lite['voices'] if v['lang'] == lang]
                   for lang in ('a', 'b')},
    }


CATALOGUE = load_catalogue()
VOICE_BY_ID = {v['id']: v for v in CATALOGUE}
# Kokoro's own ids (e.g. af_heart) are still accepted from API callers written before the
# catalogue; they map to the Voxa voice built from that one Kokoro voice.
KOKORO_ALIASES = {v['source']: v['id'] for v in CATALOGUE if isinstance(v['source'], str)}
# Voxa voice ids per language; the first is the language's default.
VOICES = {lang: [v['id'] for v in CATALOGUE if v['lang'] == lang] for lang in LANG_LABELS}

# Voice previews: one fixed line per language, so each Studio voice is generated at most once
# and kept on disk. Free for everyone, since nobody can choose the text. Names are left out
# where the script is not Latin, so the pronunciation stays clean.
PREVIEW_LINES = {
    'a': "Hi, I'm {name}. This is how your words will sound in my voice.",
    'b': "Hello, I'm {name}. This is how your words will sound in my voice.",
    'e': 'Hola, soy {name}. Así sonarán tus palabras con mi voz.',
    'f': 'Bonjour, je suis {name}. Voici comment vos mots sonneront avec ma voix.',
    'h': 'नमस्ते! आपके शब्द मेरी आवाज़ में कुछ ऐसे सुनाई देंगे।',
    'i': 'Ciao, sono {name}. Ecco come suoneranno le tue parole con la mia voce.',
    'j': 'こんにちは。あなたの言葉は、私の声でこんなふうに聞こえます。',
    'p': 'Olá, eu sou {name}. É assim que as suas palavras vão soar na minha voz.',
    'z': '你好！你的文字用我的声音读出来，就是这样的。',
}
PREVIEW_WAIT = 90


def preview_text(voice_id):
    v = VOICE_BY_ID[voice_id]
    return PREVIEW_LINES[v['lang']].format(name=v['name'])
LITE = load_lite(studio_ids=VOICE_BY_ID)

_shared_model = None
_pipelines = {}
_blends = {}
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
            _shared_model = KModel(repo_id='hexgrad/Kokoro-82M').to(DEVICE).eval()
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


def kokoro_voice(pipe, voice_id):
    """What Kokoro takes for a Voxa voice: its voice id, or a weighted blend tensor.

    Kokoro's own "a,b" syntax only averages voices equally, so blends are summed here,
    once per process."""
    src = VOICE_BY_ID[voice_id]['source']
    if isinstance(src, str):
        return src
    with _pipe_lock:
        if voice_id not in _blends:
            _blends[voice_id] = sum(w * pipe.load_single_voice(k) for k, w in src.items())
        return _blends[voice_id]


def synth(text, voice, speed, lang_code, on_progress=None):
    """Yield one WAV (bytes) per sentence chunk, reporting the share of text consumed."""
    import numpy as np
    import soundfile as sf

    pipe = get_pipeline(lang_code)
    consumed = 0
    for graphemes, _, audio in pipe(text, voice=kokoro_voice(pipe, voice), speed=speed):
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
    if not isinstance(voice, str):
        raise BadRequest('voice must be a string')
    voice = KOKORO_ALIASES.get(voice, voice)
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


class AuthError(Exception):
    """A sign-in token we cannot accept."""


class SignInRequired(Exception):
    """Sign-in is configured and this request came from a visitor who is not signed in."""

    def __init__(self, what='use Voxa'):
        super().__init__(f'Sign in to {what}. A Voxa Lite account is free.')


def _b64url(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def _verify_asymmetric(jwk, alg, signed, signature):
    """Check an ES256 or RS256 signature against one key from the project's JWKS."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils

    def num(field):
        return int.from_bytes(_b64url(jwk[field]), 'big')

    try:
        if alg == 'ES256' and jwk.get('kty') == 'EC' and jwk.get('crv') == 'P-256':
            if len(signature) != 64:
                return False
            key = ec.EllipticCurvePublicNumbers(num('x'), num('y'), ec.SECP256R1()).public_key()
            der = utils.encode_dss_signature(int.from_bytes(signature[:32], 'big'),
                                             int.from_bytes(signature[32:], 'big'))
            key.verify(der, signed, ec.ECDSA(hashes.SHA256()))
            return True
        if alg == 'RS256' and jwk.get('kty') == 'RSA':
            key = rsa.RSAPublicNumbers(num('e'), num('n')).public_key()
            key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
            return True
    except (InvalidSignature, KeyError, ValueError):
        pass
    return False


class TokenVerifier:
    """Checks Supabase access tokens: signature, expiry, issuer, audience and role."""

    LEEWAY = 30  # seconds of clock skew tolerated on expiry

    def __init__(self, url, hs256_secret='', fetch_jwks=None, clock=time.time):
        self.issuer = f'{url}/auth/v1'
        self.jwks_url = f'{url}/auth/v1/.well-known/jwks.json'
        self.secret = hs256_secret.encode() if hs256_secret else None
        self._fetch = fetch_jwks or self._fetch_jwks
        self._clock = clock
        self._keys = {}
        self._fetched_at = None
        self._lock = threading.Lock()

    def _fetch_jwks(self):
        with urllib.request.urlopen(self.jwks_url, timeout=5) as r:
            return json.load(r)

    def _key(self, kid):
        """The signing key with this id. An unknown id means the project rotated its keys,
        so the JWKS is fetched again, at most once a minute."""
        with self._lock:
            now = self._clock()
            stale = self._fetched_at is None or now - self._fetched_at > 60
            if kid not in self._keys and stale:
                self._fetched_at = now
                try:
                    jwks = self._fetch()
                    self._keys = {k['kid']: k for k in jwks.get('keys', []) if k.get('kid')}
                except Exception as e:
                    log.warning('cannot fetch signing keys from %s: %s', self.jwks_url, e)
            return self._keys.get(kid)

    def verify(self, token):
        """Return the token's claims, or raise AuthError."""
        try:
            head, body, sig = token.split('.')
            header = json.loads(_b64url(head))
            claims = json.loads(_b64url(body))
            signature = _b64url(sig)
        except (ValueError, binascii.Error, UnicodeDecodeError):
            raise AuthError('malformed token')
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise AuthError('malformed token')
        signed = f'{head}.{body}'.encode()
        alg = header.get('alg')
        if alg == 'HS256' and self.secret:
            valid = hmac.compare_digest(
                hmac.new(self.secret, signed, hashlib.sha256).digest(), signature)
        elif alg in ('ES256', 'RS256'):
            jwk = self._key(header.get('kid'))
            valid = jwk is not None and _verify_asymmetric(jwk, alg, signed, signature)
        else:
            valid = False
        if not valid:
            raise AuthError('bad signature')
        exp = claims.get('exp')
        if not isinstance(exp, (int, float)) or exp < self._clock() - self.LEEWAY:
            raise AuthError('expired')
        if claims.get('iss') != self.issuer:
            raise AuthError('wrong issuer')
        aud = claims.get('aud')
        if 'authenticated' not in (aud if isinstance(aud, list) else [aud]):
            raise AuthError('wrong audience')
        # The project's anon key is itself a valid token with role "anon"; it is not a user.
        if claims.get('role') != 'authenticated' or not claims.get('sub'):
            raise AuthError('not a signed-in user')
        return claims


def identity_from_claims(claims, ip):
    # The tier comes from app_metadata, which only the server side (service role, Paddle
    # webhook) can write. user_metadata is editable by the user, so it is never trusted.
    app = claims.get('app_metadata') if isinstance(claims.get('app_metadata'), dict) else {}
    tier = 'paid' if app.get('tier') == 'paid' else 'free'
    return Identity(ip, user=claims['sub'], tier=tier, email=claims.get('email'),
                    customer=app.get('paddle_customer'), subscription=app.get('paddle_subscription'))


_auth = None  # a TokenVerifier once sign-in is configured


class QuotaExceeded(Exception):
    def __init__(self, status, requested):
        self.status = status
        self.requested = requested
        left = status['remaining']
        hours = max(1, round((status['resets_at'] - time.time()) / 3600))
        super().__init__(
            f'Daily limit reached: {left:,} of {status["limit"]:,} characters left today '
            f'and this script is {requested:,}. It resets at midnight UTC '
            f'(in about {hours} hour{"" if hours == 1 else "s"}).'
        )


class Identity:
    """Who a request is for. Signed-in users are counted by account; guests by their
    cookie id, or by IP when there is no cookie (scripts)."""

    def __init__(self, ip, visitor=None, user=None, tier='guest', email=None, customer=None,
                 subscription=None):
        self.ip, self.visitor, self.user, self.tier, self.email = ip, visitor, user, tier, email
        # Paddle ids, once the account has subscribed: for "Manage plan".
        self.customer, self.subscription = customer, subscription


class Quota:
    """Characters generated per identity per UTC day, in SQLite.

    `limits` maps a tier ('guest', 'free', 'paid') to its daily characters; 0 means no
    limit. Guests also count against their IP at IP_DAILY_FACTOR times the guest limit, so
    clearing cookies cannot buy unlimited use. Accounts have no IP backstop: many people
    can share one office or carrier address.
    """

    def __init__(self, path, limits, table='usage'):
        if not table.isidentifier():
            raise ValueError(table)
        self.limits = dict(limits)
        self.table = table
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(f'CREATE TABLE IF NOT EXISTS {table} ('
                         'day TEXT, subject TEXT, chars INTEGER NOT NULL, '
                         'PRIMARY KEY (day, subject))')
        self._db.commit()

    @property
    def enabled(self):
        return any(self.limits.values())

    @staticmethod
    def _today():
        now = datetime.datetime.now(datetime.timezone.utc)
        midnight = (now + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return now.date().isoformat(), midnight.timestamp()

    def _subjects(self, who):
        """(subject, daily limit) pairs this identity is counted against."""
        limit = self.limits.get(who.tier, 0)
        if who.user:
            return [(f'u:{who.user}', limit)]
        return [(f'v:{who.visitor}' if who.visitor else f'a:{who.ip}', limit),
                (f'ip:{who.ip}', limit * IP_DAILY_FACTOR)]

    def _used(self, day, subject):
        row = self._db.execute(f'SELECT chars FROM {self.table} WHERE day = ? AND subject = ?',
                               (day, subject)).fetchone()
        return row[0] if row else 0

    def _status(self, day, resets_at, who, subjects):
        limit = subjects[0][1]
        remaining = max(0, min(lim - self._used(day, subj) for subj, lim in subjects))
        return {'tier': who.tier, 'limit': limit, 'used': limit - remaining,
                'remaining': remaining, 'resets_at': int(resets_at)}

    def status(self, who):
        """Today's allowance, {'tier', 'unlimited': True} for a tier with no limit."""
        if not self.limits.get(who.tier):
            return {'tier': who.tier, 'unlimited': True}
        day, resets_at = self._today()
        with self._lock:
            return self._status(day, resets_at, who, self._subjects(who))

    def charge(self, who, n):
        """Take n characters or raise QuotaExceeded. Returns a refund callable."""
        if not self.limits.get(who.tier):
            return lambda: None
        day, resets_at = self._today()
        subjects = self._subjects(who)
        keys = [subj for subj, _ in subjects]
        with self._lock:
            status = self._status(day, resets_at, who, subjects)
            if n > status['remaining']:
                raise QuotaExceeded(status, n)
            self._add(day, keys, n)
            self._db.execute(f'DELETE FROM {self.table} WHERE day < ?', (day,))
            self._db.commit()

        def refund():
            with self._lock:
                for subject in keys:
                    self._db.execute(f'UPDATE {self.table} SET chars = MAX(0, chars - ?) '
                                     'WHERE day = ? AND subject = ?', (n, day, subject))
                self._db.commit()
        return refund

    def _add(self, day, keys, n):
        for subject in keys:
            self._db.execute(
                f'INSERT INTO {self.table} (day, subject, chars) VALUES (?, ?, ?) '
                'ON CONFLICT (day, subject) DO UPDATE SET chars = chars + excluded.chars',
                (day, subject, n))


def tier_limits():
    """Daily Studio characters per tier. Guests only generate while sign-in is not configured
    (nobody can have an account yet), and then they get Voxa Lite's allowance."""
    return {'guest': FREE_DAILY_CHARS, 'free': FREE_DAILY_CHARS, 'paid': PAID_DAILY_CHARS}


def podcast_limits():
    """Podcast scripts per tier per day; as with characters, guests get the free allowance
    until sign-in is configured."""
    return {**PODCAST_DAILY, 'guest': PODCAST_DAILY['guest'] if _auth else PODCAST_DAILY['free']}


def open_quota(limits=None, table='usage'):
    limits = tier_limits() if limits is None else limits
    path = os.path.join(DATA_DIR, 'voxa.sqlite3')
    try:
        return Quota(path, limits, table)
    except sqlite3.Error as e:
        log.warning('cannot open %s (%s); daily limits will reset on restart', path, e)
        return Quota(':memory:', limits, table)


_quota = None  # opened in main(), so importing this module touches no files
_podcast_quota = None  # scripts per day, in the same file
_scripts = podcast.ScriptJobs()


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
        self.queue = None


class JobQueue:
    """One worker thread runs every generation, so model memory never stacks up."""

    def __init__(self, generate, workers=1, name='cpu'):
        self._generate = generate
        self.workers, self.name = workers, name
        self._jobs = {}
        self._by_key = {}
        self._queue = queue.Queue()
        self._seq = itertools.count()
        self._lock = threading.Lock()

    def start(self):
        for i in range(self.workers):
            threading.Thread(target=self._run, name=f'voxa-{self.name}-{i}', daemon=True).start()

    def submit(self, text, lang, voice, speed, charge=None, who=None):
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
            job.refund, job.queue = refund, self
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


def generate_remote(job):
    """Generate on the GPU service: the same request body, a WAV back."""
    body = json.dumps({'text': job.text, 'lang': job.lang, 'voice': job.voice,
                       'speed': job.speed}).encode()
    req = urllib.request.Request(f'{GPU_URL}/synthesize', data=body, headers={
        'Content-Type': 'application/json', 'Authorization': f'Bearer {GPU_TOKEN}'})
    with urllib.request.urlopen(req, timeout=GPU_TIMEOUT) as r:
        audio = r.read()
    if not audio.startswith(b'RIFF'):
        raise RuntimeError('GPU service returned something other than a WAV')
    return audio


class JobRouter:
    """Paid users' jobs to the GPU queue, everyone else's to the CPU queue."""

    def __init__(self, cpu, gpu):
        self.cpu, self.gpu = cpu, gpu

    def start(self):
        self.cpu.start()
        self.gpu.start()

    def submit(self, text, lang, voice, speed, charge=None, who=None):
        queue = self.gpu if who is not None and who.tier == 'paid' else self.cpu
        return queue.submit(text, lang, voice, speed, charge=charge, who=who)

    def get(self, job_id):
        return self.cpu.get(job_id) or self.gpu.get(job_id)

    def ahead_of(self, job):
        return (job.queue or self.cpu).ahead_of(job)


_jobs = JobQueue(generate)


def job_json(job):
    out = {
        'id': job.id,
        'status': job.status,
        'progress': round(job.progress, 3),
        'cache': 'hit' if job.from_cache else 'miss',
    }
    if job.status == 'queued':
        out['ahead'] = (job.queue or _jobs).ahead_of(job)
    if job.status == 'done':
        out['audio_url'] = f'/api/audio/{job.key}'
    if job.error:
        out['error'] = job.error
    return out


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

    def _send_preview(self, voice_id):
        """A Studio voice saying its preview line: from disk, or generated once (uncharged)."""
        if voice_id not in VOICE_BY_ID:
            return self._send(404, {'error': 'unknown voice'})
        text = preview_text(voice_id)
        lang = VOICE_BY_ID[voice_id]['lang']
        digest = hashlib.sha256(text.encode()).hexdigest()[:10]
        folder = os.path.join(DATA_DIR, 'previews')
        path = os.path.join(folder, f'{voice_id}-{digest}.wav')
        headers = {'Cache-Control': 'public, max-age=86400', 'Cross-Origin-Resource-Policy': 'same-origin'}
        if os.path.isfile(path):
            with open(path, 'rb') as f:
                return self._send(200, f.read(), 'audio/wav', headers)
        # The text is fixed, so a preview is never charged; it goes to the CPU queue.
        job = _jobs.submit(text, lang, voice_id, 1.0)
        if not job.done.wait(PREVIEW_WAIT) or job.status != 'done':
            return self._send(503, {'error': 'The preview is not ready. Please try again in a moment.'},
                              extra={'Retry-After': '10'})
        audio = _cache.get(job.key)
        if audio is None:
            return self._send(503, {'error': 'The preview is not ready. Please try again.'})
        try:
            os.makedirs(folder, exist_ok=True)
            tmp = f'{path}.{uuid.uuid4().hex}.tmp'
            with open(tmp, 'wb') as f:
                f.write(audio)
            os.replace(tmp, path)
        except OSError as e:
            log.warning('cannot keep preview %s: %s', path, e)
        return self._send(200, audio, 'audio/wav', headers)

    def _send_static(self, rel):
        """A file under static/, by extension allow-list, never outside the directory."""
        root = os.path.realpath(STATIC_DIR)
        full = os.path.realpath(os.path.join(root, rel))
        ctype = STATIC_TYPES.get(os.path.splitext(full)[1])
        if not full.startswith(root + os.sep) or not ctype or not os.path.isfile(full):
            return self._send(404, {'error': 'not found'})
        with open(full, 'rb') as f:
            body = f.read()
        # Paths carry the package version, so a file never changes under its URL. Workers
        # take their isolation from their own script's headers, so these carry it too.
        return self._send(200, body, ctype, {
            'Cache-Control': 'public, max-age=31536000, immutable',
            'Cross-Origin-Resource-Policy': 'same-origin', **ISOLATION})

    def _send_auth_error(self, e):
        log.info('rejected sign-in token: %s', e)
        self.close_connection = True  # a rejected POST body is never read
        self._send(401, {'error': 'Your sign-in has expired. Please sign in again.'},
                   extra={'WWW-Authenticate': 'Bearer error="invalid_token"'})

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

    def _identity(self):
        """The signed-in user from the Authorization header, else a guest. A token that is
        present but invalid or expired is an AuthError, not a silent fall back to guest."""
        ip = self._client_ip()
        header = self.headers.get('Authorization') or ''
        if _auth and header.startswith('Bearer '):
            return identity_from_claims(_auth.verify(header[7:].strip()), ip)
        return Identity(ip, visitor=self._visitor())

    def _submit(self):
        who = self._identity()
        if _auth and not who.user:
            raise SignInRequired('generate speech')
        charge = (lambda n: _quota.charge(who, n)) if _quota else None
        job = _jobs.submit(*self._read_request(), charge=charge, who=who)
        return job, who

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
        except Busy:
            self._send(503, {'error': 'The server is busy. Please try again in a minute.'},
                       extra={'Retry-After': '30'})
        except AuthError as e:
            self._send_auth_error(e)
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
        except AuthError as e:
            self._send_auth_error(e)
        except SignInRequired as e:
            # The page opens its sign-in box on `signin`; the message stands on its own too.
            self._send(403, {'error': str(e), 'signin': True})
        except QuotaExceeded as e:
            self._send(429, {'error': str(e), 'quota': e.status},
                       extra={'Retry-After': str(max(1, int(e.status['resets_at'] - time.time())))})
        except Exception:
            log.exception('POST %s failed', self.path)
            self._send(500, {'error': 'internal server error'})

    def _get(self):
        p = urlparse(self.path).path
        if p == '/index.html':
            p = '/'
        if p.startswith('/static/'):
            return self._send_static(p[len('/static/'):])
        if p in WEB_FILES:
            with open(os.path.join(WEB_DIR, WEB_FILES[p]), 'rb') as f:
                return self._send(200, f.read(), 'image/png', {'Cache-Control': 'public, max-age=86400'})
        if p in WEB_PAGES:
            name = WEB_PAGES[p]
            with open(os.path.join(WEB_DIR, name), encoding='utf-8') as f:
                page = f.read()
            for key, value in web_values().items():
                page = page.replace('{{' + key + '}}', value)
            ctype = {'.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
                     '.webmanifest': 'application/manifest+json'}.get(
                os.path.splitext(name)[1], 'text/html; charset=utf-8')
            # Isolated so on-device voices get WebAssembly threads (all but PLAIN_PAGES).
            extra = {**({} if p in PLAIN_PAGES else ISOLATION), 'Cache-Control': 'no-cache'}
            if p == '/' and not self._visitor():
                extra['Set-Cookie'] = self._new_visitor_cookie()
            return self._send(200, page, ctype, extra)
        if p == '/api/quota':
            who = self._identity()
            status = _quota.status(who) if _quota else None
            account = {'email': who.email, 'tier': who.tier} if who.user else None
            return self._send(200, {'quota': status, 'account': account},
                              extra={'Cache-Control': 'no-store'})
        if p == '/api/config':
            auth = {'url': SUPABASE_URL, 'anon_key': SUPABASE_ANON_KEY} if _auth else None
            paying = ({'price_label': billing.PRICE_LABEL, 'paddle': {
                'token': billing.PADDLE_CLIENT_TOKEN, 'environment': 'sandbox' if billing.sandbox() else 'production'}}
                if _auth and billing.enabled(SUPABASE_URL) else None)
            pods = {'daily': podcast_limits(), 'minutes': sorted(podcast.LENGTHS),
                    'max_minutes': {**PODCAST_MAX_MINUTES, 'guest': PODCAST_MAX_MINUTES['free']},
                    'tones': list(podcast.TONES), 'max_text': podcast.MAX_SOURCE} if podcast.enabled() else None
            return self._send(200, {'auth': auth, 'limits': tier_limits(), 'tiers': TIERS,
                                    'billing': paying, 'podcast': pods})
        if p.startswith('/api/podcast/script/'):
            if not podcast.enabled():
                return self._send(404, {'error': 'podcasts are not set up'})
            job = _scripts.get(p.rsplit('/', 1)[-1], self._owner(self._identity()))
            if job is None:
                return self._send(404, {'error': 'script not found; the server may have restarted'})
            return self._send(200, job.to_json(), extra={'Cache-Control': 'no-store'})
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
        if p.startswith('/api/preview/'):
            return self._send_preview(p.rsplit('/', 1)[-1])
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
            catalogue = {lang: [{k: VOICE_BY_ID[vid][k]
                                 for k in ('id', 'name', 'description', 'gender', 'signature')}
                                for vid in ids]
                         for lang, ids in VOICES.items()}
            return self._send(200, {'voices': VOICES, 'catalogue': catalogue, 'lite': LITE,
                                    'phonon_version': PHONON_VERSION,
                                    'languages': LANG_LABELS})
        return self._send(404, {'error': 'not found'})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _billing(self, p):
        """Upgrade, manage the plan, and Paddle's webhook."""
        if not (_auth and billing.enabled(SUPABASE_URL)):
            return self._send(404, {'error': 'payments are not set up'})
        if p == '/api/billing/webhook':
            n = int(self.headers.get('Content-Length') or 0)
            if n > 256 * 1024:
                self.close_connection = True
                return self._send(413, {'error': 'too large'})
            payload = self.rfile.read(n)
            try:
                event = billing.verify(payload, self.headers.get('Paddle-Signature'))
            except (billing.BadSignature, ValueError) as e:
                log.warning('rejected Paddle webhook: %s', e)
                return self._send(400, {'error': 'bad signature'})
            try:
                result = billing.handle_event(event, SUPABASE_URL)
            except Exception:
                # A non-2xx makes Paddle retry the event later, which is what we want here.
                log.exception('Paddle event %s failed', event.get('event_id'))
                return self._send(500, {'error': 'could not apply the event'})
            log.info('Paddle %s %s: %s', event.get('event_type'), event.get('event_id'), result)
            return self._send(200, {'received': True})
        who = self._identity()
        if not who.user:
            return self._send(401, {'error': 'Sign in first.'})
        origin = PUBLIC_URL or f"{self.headers.get('X-Forwarded-Proto', 'http')}://{self.headers.get('Host', 'localhost')}"
        try:
            if p == '/api/billing/checkout':
                if who.tier == 'paid':
                    return self._send(409, {'error': 'This account already has the paid plan.'})
                return self._send(200, {'checkout': billing.checkout(who, origin + '/')})
            if p == '/api/billing/portal':
                if not who.customer:
                    return self._send(409, {'error': 'There is no subscription on this account yet.'})
                return self._send(200, {'url': billing.portal_url(who)})
        except billing.BillingError as e:
            return self._send(502, {'error': str(e)})
        return self._send(404, {'error': 'not found'})

    @staticmethod
    def _owner(who):
        return f'u:{who.user}' if who.user else f'v:{who.visitor}' if who.visitor else f'a:{who.ip}'

    def _podcast_script(self):
        """Start writing a podcast script from a document's text; the page polls for it."""
        if not podcast.enabled():
            return self._send(404, {'error': 'podcasts are not set up'})
        who = self._identity()
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            raise BadRequest('bad Content-Length')
        if n > MAX_PODCAST_BODY:
            self.close_connection = True
            raise BadRequest('The document is too long. Try fewer pages.')
        try:
            req = podcast.parse_request(json.loads(self.rfile.read(n) or b'{}'))
        except (ValueError, UnicodeDecodeError) as e:  # podcast.BadRequest is a ValueError
            raise BadRequest(str(e) if isinstance(e, podcast.BadRequest) else f'bad json: {e}')
        if _auth and not who.user:
            raise SignInRequired('make podcasts')
        limits = podcast_limits()
        if not limits.get(who.tier):
            return self._send(403, {'error': f'Podcasts are part of {TIERS["paid"]}.'})
        longest = PODCAST_MAX_MINUTES.get(who.tier, PODCAST_MAX_MINUTES['free'])
        if req['minutes'] > longest:
            return self._send(403, {'error': f'{TIERS.get(who.tier, TIERS["free"])} episodes are up to '
                                             f'{longest} minutes. {TIERS["paid"]} goes up to '
                                             f'{PODCAST_MAX_MINUTES["paid"]}.'})
        try:
            refund = _podcast_quota.charge(who, 1) if _podcast_quota else (lambda: None)
        except QuotaExceeded as e:
            hours = max(1, round((e.status['resets_at'] - time.time()) / 3600))
            word = 'script' if e.status['limit'] == 1 else 'scripts'
            return self._send(429, {'error': f'You have made your {e.status["limit"]} podcast {word} '
                                             f'for today. More in about {hours} h (midnight UTC).'},
                              extra={'Retry-After': str(hours * 3600)})
        try:
            job = _scripts.submit(req, self._owner(who), refund)
        except podcast.Busy:
            refund()
            raise Busy()
        log.info('podcast script %s: %s chars, %s min, %s', job.id, len(req['text']),
                 req['minutes'], req['lang'])
        return self._send(202, job.to_json())

    def _post(self):
        p = urlparse(self.path).path
        if p.startswith('/api/billing/'):
            return self._billing(p)
        if p == '/api/podcast/script':
            return self._podcast_script()
        if p == '/api/jobs':
            job, who = self._submit()
            out = job_json(job)
            if _quota:
                out['quota'] = _quota.status(who)
            return self._send(200 if job.status == 'done' else 202, out)
        if p == '/api/tts':
            # Synchronous form for scripts and API callers: same queue, waits for the result.
            job, _ = self._submit()
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
    global _auth, _quota, _podcast_quota, _jobs
    if SUPABASE_URL and SUPABASE_ANON_KEY:
        _auth = TokenVerifier(SUPABASE_URL, SUPABASE_JWT_SECRET)
        log.info('sign-in enabled via %s', SUPABASE_URL)
    _quota = open_quota()
    log.info('daily characters per tier (0 = no limit): %s', _quota.limits)
    if podcast.enabled():
        _podcast_quota = open_quota(podcast_limits(), 'podcast_usage')
        log.info('podcasts on (%s); scripts per day per tier: %s', podcast.MODEL, _podcast_quota.limits)
    if GPU_URL and GPU_TOKEN:
        _jobs = JobRouter(_jobs, JobQueue(generate_remote, workers=GPU_WORKERS, name='gpu'))
        log.info('paid jobs go to the GPU service at %s', GPU_URL)
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
