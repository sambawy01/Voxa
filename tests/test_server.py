"""Server tests that run without torch or the model: generation is stubbed out.

    python -m unittest discover -s tests
"""
import base64
import hashlib
import hmac
import io
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import wave
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import server  # noqa: E402


def make_wav(seconds=0.1, rate=24000):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b'\x01\x00' * int(seconds * rate))
    return buf.getvalue()


class FakeGenerator:
    def __init__(self):
        self.calls = []
        self.gate = threading.Event()
        self.gate.set()
        self.fail = False

    def __call__(self, job):
        self.calls.append(job)
        job.progress = 0.5
        self.gate.wait(10)
        if self.fail:
            raise RuntimeError('boom: /secret/internal/path')
        return make_wav()


class ParseRequestTest(unittest.TestCase):
    def ok(self, **body):
        return server.parse_request(body)

    def bad(self, body, fragment):
        with self.assertRaises(server.BadRequest) as cm:
            server.parse_request(body)
        self.assertIn(fragment, str(cm.exception))

    def test_defaults_voice_and_speed(self):
        self.assertEqual(self.ok(text=' hi '), ('hi', 'a', 'harper', 1.0))
        self.assertEqual(self.ok(text='hola', lang='e', voice=None, speed=None),
                         ('hola', 'e', 'lucia', 1.0))

    def test_every_language_has_a_default_voice(self):
        for lang in server.LANG_LABELS:
            self.assertEqual(self.ok(text='x', lang=lang)[2], server.VOICES[lang][0])

    def test_rejects_bad_text(self):
        self.bad({'text': '   '}, 'empty')
        self.bad({'text': 123}, 'string')
        self.bad({}, 'string')
        self.bad({'text': 'a' * (server.MAX_TEXT + 1)}, 'longer')
        self.bad(['not', 'an', 'object'], 'JSON object')

    def test_accepts_kokoro_ids_from_older_callers(self):
        self.assertEqual(self.ok(text='x', voice='af_bella')[2], 'sienna')
        self.assertEqual(self.ok(text='x', lang='b', voice='bf_emma')[2], 'eleanor')

    def test_rejects_unknown_language_and_voice(self):
        self.bad({'text': 'x', 'lang': 'xx'}, 'unsupported language')
        self.bad({'text': 'x', 'voice': 'nope'}, 'not available')
        self.bad({'text': 'x', 'lang': 'f', 'voice': 'am_adam'}, 'not available for French')

    def test_speed_is_bounded(self):
        # 0.01 used to OOM-kill the machine; 50 crashed the request.
        for s in (0.01, 0.49, 2.01, 50, -5, 0, float('inf'), float('nan'), 'fast', True, [1]):
            with self.subTest(speed=s):
                self.bad({'text': 'x', 'speed': s}, 'speed')
        self.assertEqual(self.ok(text='x', speed='0.9')[3], 0.9)
        self.assertEqual(self.ok(text='x', speed=2)[3], 2.0)


class CatalogueTest(unittest.TestCase):
    def test_covers_every_kokoro_voice_once(self):
        singles = [v['source'] for v in server.CATALOGUE if isinstance(v['source'], str)]
        self.assertEqual(len(singles), 54)
        self.assertEqual(len(set(singles)), 54)

    def test_every_language_has_voices_and_a_default(self):
        for lang in server.LANG_LABELS:
            self.assertTrue(server.VOICES[lang], lang)
        self.assertEqual(server.VOICES['a'][0], 'harper')
        self.assertEqual(server.VOICES['b'][0], 'eleanor')

    def test_signature_blends(self):
        aurora = server.VOICE_BY_ID['aurora']
        self.assertEqual(aurora['source'], {'af_heart': 0.6, 'af_bella': 0.4})
        self.assertIn('signature', aurora['description'])

    def test_bad_catalogues_are_rejected(self):
        import tempfile
        good = {'id': 'x', 'name': 'X', 'lang': 'a', 'gender': 'female', 'accent': 'American',
                'grade': 'A', 'source': 'af_heart'}
        cases = {
            'duplicate': [good, dict(good)],
            'another language': [dict(good, source={'af_heart': 0.5, 'bf_emma': 0.5})],
            'add up to 1': [dict(good, source={'af_heart': 0.5, 'af_bella': 0.4})],
            'unknown language': [dict(good, lang='xx')],
        }
        for message, voices in cases.items():
            with self.subTest(message), tempfile.NamedTemporaryFile('w', suffix='.json') as f:
                json.dump({'voices': voices}, f)
                f.flush()
                with self.assertRaisesRegex(ValueError, message):
                    server.load_catalogue(f.name)


class AudioCacheTest(unittest.TestCase):
    def test_bounded_by_bytes_lru(self):
        c = server.AudioCache(100)
        c.put('a', b'x' * 40)
        c.put('b', b'x' * 40)
        c.get('a')  # a is now most recent
        c.put('c', b'x' * 40)
        self.assertIsNone(c.get('b'))
        self.assertIsNotNone(c.get('a'))
        self.assertIsNotNone(c.get('c'))
        self.assertLessEqual(c.nbytes, 100)

    def test_oversized_item_is_not_cached(self):
        c = server.AudioCache(10)
        c.put('big', b'x' * 11)
        self.assertEqual(len(c), 0)

    def test_replacing_a_key_keeps_byte_count_right(self):
        c = server.AudioCache(100)
        c.put('a', b'x' * 30)
        c.put('a', b'x' * 50)
        self.assertEqual(c.nbytes, 50)


class QuotaTest(unittest.TestCase):
    def setUp(self):
        self.q = server.Quota(':memory:', {'guest': 100, 'free': 400, 'paid': 0})

    @staticmethod
    def guest(visitor='v1', ip='ip1'):
        return server.Identity(ip, visitor=visitor)

    def test_charge_until_exhausted(self):
        self.q.charge(self.guest(), 60)
        self.assertEqual(self.q.status(self.guest())['remaining'], 40)
        with self.assertRaises(server.QuotaExceeded) as cm:
            self.q.charge(self.guest(), 41)
        self.assertIn('40 of 100', str(cm.exception))
        self.q.charge(self.guest(), 40)
        self.assertEqual(self.q.status(self.guest())['remaining'], 0)

    def test_refund(self):
        refund = self.q.charge(self.guest(), 70)
        refund()
        self.assertEqual(self.q.status(self.guest())['remaining'], 100)
        refund()  # never goes below zero used
        self.assertEqual(self.q.status(self.guest())['used'], 0)

    def test_guests_are_separate_but_share_an_ip_backstop(self):
        for i in range(server.IP_DAILY_FACTOR):
            self.q.charge(self.guest(f'v{i}', 'shared'), 100)
        with self.assertRaises(server.QuotaExceeded):
            self.q.charge(self.guest('another', 'shared'), 1)
        self.q.charge(self.guest('another', 'elsewhere'), 100)

    def test_no_cookie_counts_by_ip(self):
        self.q.charge(server.Identity('ip1'), 100)
        with self.assertRaises(server.QuotaExceeded):
            self.q.charge(server.Identity('ip1'), 1)
        self.q.charge(self.guest('has-cookie', 'ip1'), 1)  # a browser on that IP has its own

    def test_accounts_get_their_tier_and_no_ip_backstop(self):
        for i in range(10):  # ten free accounts behind one office address
            user = server.Identity('office', user=f'u{i}', tier='free')
            self.q.charge(user, 400)
            self.assertEqual(self.q.status(user)['remaining'], 0)
        paid = server.Identity('office', user='rich', tier='paid')
        self.q.charge(paid, 10 ** 6)()
        self.assertEqual(self.q.status(paid), {'tier': 'paid', 'unlimited': True})

    def test_resets_each_day(self):
        days = iter([('2026-10-22', 0), ('2026-10-22', 0), ('2026-10-23', 0)])
        self.q._today = lambda: next(days)
        self.q.charge(self.guest(), 100)
        with self.assertRaises(server.QuotaExceeded):
            self.q.charge(self.guest(), 1)
        self.q.charge(self.guest(), 100)

    def test_zero_limits_disable(self):
        q = server.Quota(':memory:', {'guest': 0, 'free': 0, 'paid': 0})
        self.assertFalse(q.enabled)
        q.charge(self.guest(), 10 ** 9)()

    def test_guests_get_the_free_allowance_until_sign_in_exists(self):
        self.assertIsNone(server._auth)
        self.assertEqual(server.tier_limits()['guest'], server.FREE_DAILY_CHARS)


class JoinWavsTest(unittest.TestCase):
    def test_joins_frames(self):
        joined = server.join_wavs([make_wav(0.1), make_wav(0.2)])
        with wave.open(io.BytesIO(joined)) as w:
            self.assertEqual(w.getnframes(), int(0.1 * 24000) + int(0.2 * 24000))

    def test_empty(self):
        self.assertEqual(server.join_wavs([]), b'')

    def test_mismatch(self):
        with self.assertRaises(ValueError):
            server.join_wavs([make_wav(rate=24000), make_wav(rate=16000)])


class ServerTestCase(unittest.TestCase):
    """A real HTTP server on a free port, with generation stubbed by FakeGenerator."""

    @classmethod
    def setUpClass(cls):
        cls.gen = FakeGenerator()
        server._jobs = server.JobQueue(cls.gen)
        server._jobs.start()
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.base = f'http://127.0.0.1:{cls.httpd.server_address[1]}'
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        self.gen.gate.set()
        self.gen.fail = False
        server._cache = server.AudioCache(server.MAX_CACHE_BYTES)

    def req(self, path, body=None, raw=None, cookie=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {'Content-Type': 'application/json'}
        if cookie:
            headers['Cookie'] = cookie
        r = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def poll(self, job_id):
        for _ in range(100):
            code, _, body = self.req(f'/api/jobs/{job_id}')
            job = json.loads(body)
            if job['status'] in ('done', 'error'):
                return job
            time.sleep(0.05)
        self.fail('job never finished')


class HTTPTest(ServerTestCase):
    def test_page_and_metadata(self):
        code, headers, body = self.req('/')
        self.assertEqual(code, 200)
        self.assertIn(b'/api/jobs', body)
        code, _, body = self.req('/api/voices')
        voices = json.loads(body)
        self.assertEqual(sum(len(v) for v in voices['voices'].values()), 57)
        first = voices['catalogue']['a'][0]
        self.assertEqual((first['id'], first['name']), ('harper', 'Harper'))
        self.assertEqual(first['description'], 'Female · American · Best quality')
        code, _, body = self.req('/health')
        self.assertTrue(json.loads(body)['ok'])

    def test_job_flow_then_cache_hit(self):
        code, _, body = self.req('/api/jobs', {'text': 'job flow', 'speed': 0.9})
        self.assertIn(code, (200, 202))  # 200 if the stub finishes before the reply
        self.assertEqual(json.loads(body)['cache'], 'miss')
        job = self.poll(json.loads(body)['id'])
        self.assertEqual(job['status'], 'done')
        code, headers, audio = self.req(job['audio_url'])
        self.assertEqual(code, 200)
        self.assertTrue(audio.startswith(b'RIFF'))

        calls = len(self.gen.calls)
        code, _, body = self.req('/api/jobs', {'text': 'job flow', 'speed': 0.9})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['cache'], 'hit')
        self.assertEqual(len(self.gen.calls), calls)

    def test_duplicate_submission_joins_running_job(self):
        self.gen.gate.clear()
        _, _, a = self.req('/api/jobs', {'text': 'double tap'})
        _, _, b = self.req('/api/jobs', {'text': 'double tap'})
        self.assertEqual(json.loads(a)['id'], json.loads(b)['id'])
        self.gen.gate.set()
        self.assertEqual(self.poll(json.loads(a)['id'])['status'], 'done')

    def test_sync_tts(self):
        code, headers, audio = self.req('/api/tts', {'text': 'sync', 'lang': 'z'})
        self.assertEqual(code, 200)
        self.assertEqual(headers['X-Voxa-Cache'], 'miss')
        self.assertTrue(audio.startswith(b'RIFF'))
        code, headers, _ = self.req('/api/tts', {'text': 'sync', 'lang': 'z'})
        self.assertEqual(headers['X-Voxa-Cache'], 'hit')

    def test_generation_failure_is_a_clean_500_without_internals(self):
        self.gen.fail = True
        code, _, body = self.req('/api/tts', {'text': 'will fail'})
        self.assertEqual(code, 500)
        self.assertNotIn(b'secret', body)
        # The worker survives a failed job.
        self.gen.fail = False
        code, _, _ = self.req('/api/tts', {'text': 'after failure'})
        self.assertEqual(code, 200)

    def test_bad_input_is_400_not_a_dropped_connection(self):
        cases = [
            {'text': 'x', 'speed': 'fast'},
            {'text': 'x', 'speed': 0.01},
            {'text': 123},
            {'text': 'x', 'lang': 'xx'},
            {'text': 'x', 'voice': 'nope'},
        ]
        for body in cases:
            for path in ('/api/tts', '/api/jobs'):
                with self.subTest(path=path, body=body):
                    code, _, resp = self.req(path, body)
                    self.assertEqual(code, 400)
                    self.assertIn('error', json.loads(resp))
        code, _, _ = self.req('/api/tts', raw=b'not json')
        self.assertEqual(code, 400)
        code, _, _ = self.req('/api/tts', raw=b'\xff\xfe')
        self.assertEqual(code, 400)

    def test_oversized_body_rejected(self):
        code, _, _ = self.req('/api/tts', raw=b'{"text":"' + b'a' * server.MAX_BODY + b'"}')
        self.assertEqual(code, 400)

    def test_queue_limit_returns_503(self):
        self.gen.gate.clear()
        ids = []
        try:
            for i in range(server.MAX_PENDING):
                code, _, body = self.req('/api/jobs', {'text': f'fill {i}'})
                self.assertEqual(code, 202)
                ids.append(json.loads(body)['id'])
            code, headers, _ = self.req('/api/jobs', {'text': 'one too many'})
            self.assertEqual(code, 503)
            self.assertIn('Retry-After', headers)
            _, _, body = self.req(f'/api/jobs/{ids[-1]}')
            self.assertEqual(json.loads(body)['ahead'], server.MAX_PENDING - 1)
        finally:
            self.gen.gate.set()
        for i in ids:
            self.poll(i)

    def test_unknown_job_and_audio(self):
        self.assertEqual(self.req('/api/jobs/nope')[0], 404)
        self.assertEqual(self.req('/api/audio/nope')[0], 404)
        self.assertEqual(self.req('/nope')[0], 404)


if __name__ == '__main__':
    unittest.main()


class QuotaHTTPTest(ServerTestCase):
    """The free-tier limit, end to end, with a 50-character limit."""

    def setUp(self):
        super().setUp()
        server._quota = server.Quota(':memory:', {'guest': 50, 'free': 50, 'paid': 0})

    def tearDown(self):
        server._quota = None

    def test_quota_page_sets_a_visitor_cookie_once(self):
        _, headers, _ = self.req('/')
        cookie = headers['Set-Cookie'].split(';')[0]
        self.assertRegex(cookie, r'^voxa_id=[0-9a-f]{32}$')
        _, headers, _ = self.req('/', cookie=cookie)
        self.assertNotIn('Set-Cookie', headers)

    def test_quota_charged_reported_and_enforced(self):
        cookie = 'voxa_id=' + 'a' * 32
        code, _, body = self.req('/api/jobs', {'text': 'x' * 30}, cookie=cookie)
        # 200 when the stub finishes before the reply is written; either is correct.
        self.assertIn(code, (200, 202))
        self.assertEqual(json.loads(body)['cache'], 'miss')
        self.assertEqual(json.loads(body)['quota']['remaining'], 20)
        self.poll(json.loads(body)['id'])

        code, headers, body = self.req('/api/jobs', {'text': 'y' * 21}, cookie=cookie)
        self.assertEqual(code, 429)
        self.assertIn('Retry-After', headers)
        self.assertIn('20 of 50', json.loads(body)['error'])

        # Replaying audio that is already cached is free, even with too little left.
        self.req('/api/jobs', {'text': 'z' * 20}, cookie=cookie)
        code, _, body = self.req('/api/jobs', {'text': 'x' * 30}, cookie=cookie)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['cache'], 'hit')

        _, _, body = self.req('/api/quota', cookie=cookie)
        self.assertEqual(json.loads(body)['quota']['remaining'], 0)
        # A different visitor still has a full allowance.
        _, _, body = self.req('/api/quota', cookie='voxa_id=' + 'b' * 32)
        self.assertEqual(json.loads(body)['quota']['remaining'], 50)

    def test_quota_refunded_when_generation_fails(self):
        self.gen.fail = True
        code, _, _ = self.req('/api/tts', {'text': 'q' * 40})
        self.assertEqual(code, 500)
        _, _, body = self.req('/api/quota')
        self.assertEqual(json.loads(body)['quota']['remaining'], 50)

    def test_quota_double_tap_is_charged_once(self):
        self.gen.gate.clear()
        cookie = 'voxa_id=' + 'c' * 32
        _, _, a = self.req('/api/jobs', {'text': 'd' * 30}, cookie=cookie)
        code, _, b = self.req('/api/jobs', {'text': 'd' * 30}, cookie=cookie)
        self.assertEqual(code, 202)
        self.assertEqual(json.loads(a)['id'], json.loads(b)['id'])
        self.gen.gate.set()
        self.poll(json.loads(a)['id'])
        _, _, body = self.req('/api/quota', cookie=cookie)
        self.assertEqual(json.loads(body)['quota']['remaining'], 20)

    def test_quota_cookie_ignored_unless_well_formed(self):
        self.req('/api/tts', {'text': 'e' * 50}, cookie='voxa_id=not-a-real-id')
        # Counted against the IP instead, so a cookie-less request is now out of allowance.
        code, _, _ = self.req('/api/tts', {'text': 'f'})
        self.assertEqual(code, 429)


# --- Sign-in tokens -------------------------------------------------------------------

URL = 'https://example.supabase.co'


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


class Keys:
    """Signing keys made fresh for the tests, in the shapes Supabase uses."""

    def __init__(self):
        from cryptography.hazmat.primitives.asymmetric import ec, rsa
        self.ec = ec.generate_private_key(ec.SECP256R1())
        self.rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        en, rn = self.ec.public_key().public_numbers(), self.rsa.public_key().public_numbers()
        self.jwks = {'keys': [
            {'kid': 'ec1', 'kty': 'EC', 'crv': 'P-256', 'alg': 'ES256',
             'x': b64(en.x.to_bytes(32, 'big')), 'y': b64(en.y.to_bytes(32, 'big'))},
            {'kid': 'rsa1', 'kty': 'RSA', 'alg': 'RS256',
             'n': b64(rn.n.to_bytes(256, 'big')), 'e': b64(rn.e.to_bytes(3, 'big'))},
        ]}

    def token(self, alg='ES256', kid=None, secret=b'shh', **overrides):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, padding, utils
        claims = {'sub': 'user-1', 'email': 'a@example.com', 'role': 'authenticated',
                  'aud': 'authenticated', 'iss': f'{URL}/auth/v1', 'exp': time.time() + 3600}
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        header = {'alg': alg, 'typ': 'JWT'}
        if alg != 'HS256':
            header['kid'] = kid or {'ES256': 'ec1', 'RS256': 'rsa1'}[alg]
        signed = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims).encode())}"
        if alg == 'HS256':
            sig = hmac.new(secret, signed.encode(), hashlib.sha256).digest()
        elif alg == 'ES256':
            r, s = utils.decode_dss_signature(
                self.ec.sign(signed.encode(), ec.ECDSA(hashes.SHA256())))
            sig = r.to_bytes(32, 'big') + s.to_bytes(32, 'big')
        else:
            sig = self.rsa.sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f'{signed}.{b64(sig)}'


class TokenVerifierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = Keys()

    def setUp(self):
        self.fetches = 0

        def fetch():
            self.fetches += 1
            return self.keys.jwks
        self.v = server.TokenVerifier(URL, 'shh', fetch_jwks=fetch)

    def test_accepts_es256_rs256_and_hs256(self):
        for alg in ('ES256', 'RS256', 'HS256'):
            with self.subTest(alg):
                self.assertEqual(self.v.verify(self.keys.token(alg))['sub'], 'user-1')

    def test_rejects_bad_tokens(self):
        good = self.keys.token()
        head, body, sig = good.split('.')
        forged = json.loads(base64.urlsafe_b64decode(body + '=='))
        forged['app_metadata'] = {'tier': 'paid'}
        cases = {
            'tampered claims': f"{head}.{b64(json.dumps(forged).encode())}.{sig}",
            'expired': self.keys.token(exp=time.time() - 120),
            'no expiry': self.keys.token(exp=None),
            'other project': self.keys.token(iss='https://evil.supabase.co/auth/v1'),
            'wrong audience': self.keys.token(aud='someone-else'),
            'anon key, not a user': self.keys.token(role='anon'),
            'no subject': self.keys.token(sub=None),
            'wrong HS256 secret': self.keys.token('HS256', secret=b'guess'),
            'unknown key id': self.keys.token(kid='nope'),
            'alg none': b64(b'{"alg":"none"}') + f'.{body}.',
            'garbage': 'not-a-token',
        }
        for name, token in cases.items():
            with self.subTest(name), self.assertRaises(server.AuthError):
                self.v.verify(token)

    def test_hs256_is_refused_without_a_secret(self):
        v = server.TokenVerifier(URL, '', fetch_jwks=lambda: self.keys.jwks)
        with self.assertRaises(server.AuthError):
            v.verify(self.keys.token('HS256'))

    def test_keys_are_cached_and_refetched_at_most_once_a_minute(self):
        clock = [1000.0]
        v = server.TokenVerifier(URL, fetch_jwks=self.count_fetch, clock=lambda: clock[0])
        v.verify(self.keys.token(exp=2000))
        v.verify(self.keys.token(exp=2000))
        self.assertEqual(self.fetches, 1)
        for _ in range(3):  # an unknown key id does not trigger a fetch storm
            with self.assertRaises(server.AuthError):
                v.verify(self.keys.token(kid='rotated', exp=2000))
        self.assertEqual(self.fetches, 1)
        clock[0] += 61
        with self.assertRaises(server.AuthError):
            v.verify(self.keys.token(kid='rotated', exp=2000))
        self.assertEqual(self.fetches, 2)

    def count_fetch(self):
        self.fetches += 1
        return self.keys.jwks

    def test_tier_comes_only_from_app_metadata(self):
        free = self.v.verify(self.keys.token(user_metadata={'tier': 'paid'}))
        self.assertEqual(server.identity_from_claims(free, 'ip').tier, 'free')
        paid = self.v.verify(self.keys.token(app_metadata={'tier': 'paid'}))
        who = server.identity_from_claims(paid, 'ip')
        self.assertEqual((who.tier, who.user, who.email), ('paid', 'user-1', 'a@example.com'))


class AccountsHTTPTest(ServerTestCase):
    """Sign-in turned on: guests 50 a day, free accounts 100, paid unlimited."""

    def setUp(self):
        super().setUp()
        self.keys = getattr(AccountsHTTPTest, '_keys', None) or Keys()
        AccountsHTTPTest._keys = self.keys
        server._auth = server.TokenVerifier(URL, fetch_jwks=lambda: self.keys.jwks)
        server._quota = server.Quota(':memory:', {'guest': 50, 'free': 100, 'paid': 0})

    def tearDown(self):
        server._auth = None
        server._quota = None

    def bearer(self, path, body=None, token=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, headers={
            'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_free_account_gets_its_allowance_and_account_info(self):
        token = self.keys.token(sub='alice', email='alice@example.com')
        code, _, body = self.bearer('/api/jobs', {'text': 'a' * 80}, token)
        self.assertIn(code, (200, 202))
        self.assertEqual(json.loads(body)['quota']['remaining'], 20)
        _, _, body = self.bearer('/api/quota', token=token)
        body = json.loads(body)
        self.assertEqual(body['account'], {'email': 'alice@example.com', 'tier': 'free'})
        self.assertEqual(body['quota']['limit'], 100)

    def test_guest_gets_the_smaller_allowance(self):
        _, _, body = self.req('/api/quota')
        body = json.loads(body)
        self.assertIsNone(body['account'])
        self.assertEqual((body['quota']['tier'], body['quota']['limit']), ('guest', 50))
        code, _, _ = self.req('/api/tts', {'text': 'g' * 51})
        self.assertEqual(code, 429)

    def test_paid_account_is_unlimited(self):
        token = self.keys.token(sub='bob', app_metadata={'tier': 'paid'})
        code, _, body = self.bearer('/api/tts', {'text': 'p' * 400}, token)
        self.assertEqual(code, 200)
        _, _, body = self.bearer('/api/quota', token=token)
        self.assertEqual(json.loads(body)['quota'], {'tier': 'paid', 'unlimited': True})

    def test_bad_or_expired_token_is_401_not_guest(self):
        for token in (self.keys.token(exp=time.time() - 600), 'junk'):
            for path, body in (('/api/jobs', {'text': 'x'}), ('/api/quota', None)):
                with self.subTest(path=path, token=token[:10]):
                    code, headers, resp = self.bearer(path, body, token)
                    self.assertEqual(code, 401)
                    self.assertIn('sign in again', json.loads(resp)['error'])
                    self.assertIn('WWW-Authenticate', headers)

    def test_config_tells_the_page_how_to_sign_in(self):
        server.SUPABASE_URL, server.SUPABASE_ANON_KEY = URL, 'public-anon-key'
        try:
            _, _, body = self.req('/api/config')
        finally:
            server.SUPABASE_URL, server.SUPABASE_ANON_KEY = '', ''
        body = json.loads(body)
        self.assertEqual(body['auth'], {'url': URL, 'anon_key': 'public-anon-key'})
        self.assertEqual(body['limits']['guest'], server.GUEST_DAILY_CHARS)


class LiteFilesTest(ServerTestCase):
    """Voxa Lite's browser package and pages: served same-origin and cross-origin isolated."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.old = server.STATIC_DIR
        server.STATIC_DIR = self.tmp.name
        pkg = os.path.join(self.tmp.name, 'phonon-0.4.1', 'wasm')
        os.makedirs(pkg)
        for name, data in (('index.js', b'export {}'), ('wasm/x.wasm', b'\0asm'),
                           ('README.md', b'not served')):
            with open(os.path.join(self.tmp.name, 'phonon-0.4.1', name), 'wb') as f:
                f.write(data)
        with open(os.path.join(self.tmp.name, '..', 'secret.js'), 'wb') as f:
            f.write(b'outside')

    def tearDown(self):
        server.STATIC_DIR = self.old
        os.remove(os.path.join(self.tmp.name, '..', 'secret.js'))
        self.tmp.cleanup()

    def test_package_files_are_served_with_types_and_isolation(self):
        for path, ctype in (('/static/phonon-0.4.1/index.js', 'text/javascript'),
                            ('/static/phonon-0.4.1/wasm/x.wasm', 'application/wasm')):
            with self.subTest(path):
                code, headers, _ = self.req(path)
                self.assertEqual(code, 200)
                self.assertTrue(headers['Content-Type'].startswith(ctype))
                self.assertEqual(headers['Cross-Origin-Embedder-Policy'], 'require-corp')
                self.assertIn('immutable', headers['Cache-Control'])

    def test_nothing_outside_the_allow_list(self):
        for path in ('/static/phonon-0.4.1/README.md', '/static/../secret.js',
                     '/static/%2e%2e/secret.js', '/static/phonon-0.4.1/missing.js'):
            with self.subTest(path):
                self.assertEqual(self.req(path)[0], 404)

    def test_benchmark_page_is_isolated_and_pinned(self):
        code, headers, body = self.req('/lite/benchmark')
        self.assertEqual(code, 200)
        self.assertEqual(headers['Cross-Origin-Opener-Policy'], 'same-origin')
        self.assertEqual(headers['Cross-Origin-Embedder-Policy'], 'require-corp')
        self.assertIn(f'/static/phonon-{server.PHONON_VERSION}/index.js'.encode(), body)
        self.assertNotIn(b'{{PHONON_VERSION}}', body)


class LiteCatalogueTest(ServerTestCase):
    def test_lite_voices_are_offered_pinned_and_never_synthesised_by_the_server(self):
        _, headers, body = self.req('/api/voices')
        lite = json.loads(body)['lite']
        self.assertEqual([len(lite['voices'][k]) for k in ('a', 'b')], [4, 4])
        for v in lite['voices']['a'] + lite['voices']['b']:
            self.assertRegex(v['url'], r'^https://huggingface\.co/gradium/phonon-voices/resolve/[0-9a-f]{40}/voices/')
            self.assertTrue(v['description'].endswith('On this device'))
            with self.subTest(v['id']):  # the server refuses Lite voices for Studio jobs
                code, _, _ = self.req('/api/tts', {'text': 'x', 'lang': 'a', 'voice': v['id']})
                self.assertEqual(code, 400)
        self.assertIn('@', lite['model'])  # model pinned to a commit
        self.assertEqual([e['temperature'] for e in lite['expressiveness']], [0.15, 0.3, 0.5])

    def test_main_page_is_isolated_for_threads(self):
        _, headers, _ = self.req('/')
        self.assertEqual(headers['Cross-Origin-Embedder-Policy'], 'require-corp')
        self.assertEqual(headers['Cross-Origin-Opener-Policy'], 'same-origin')

    def test_bad_lite_sections_are_rejected(self):
        import tempfile
        base = {'model': 'hf://x@y', 'voices_repo': 'r', 'revision': 'z',
                'expressiveness': [{'id': 'n', 'name': 'N', 'temperature': 0.3, 'default': True}]}
        voice = {'id': 'wren', 'name': 'Wren', 'lang': 'a', 'gender': 'female',
                 'accent': 'American', 'age': 'Adult', 'file': 'voices/abc.safetensors'}
        cases = {
            'differ from Studio': ([dict(voice, id='harper')], base),
            'English only': ([dict(voice, lang='f')], base),
            'unexpected file path': ([dict(voice, file='../../etc/passwd')], base),
            'default': ([voice], dict(base, expressiveness=[{'id': 'n', 'name': 'N', 'temperature': 0.3}])),
        }
        for message, (voices, section) in cases.items():
            with self.subTest(message), tempfile.NamedTemporaryFile('w', suffix='.json') as f:
                json.dump({'voices': [], 'lite': dict(section, voices=voices)}, f)
                f.flush()
                with self.assertRaisesRegex(ValueError, message):
                    server.load_lite(f.name, studio_ids={'harper'})


class BookPagesTest(ServerTestCase):
    def test_book_page_and_module_are_isolated_and_versioned(self):
        code, headers, page = self.req('/book')
        self.assertEqual(code, 200)
        self.assertEqual(headers['Cross-Origin-Embedder-Policy'], 'require-corp')
        self.assertIn(f'/static/pdfjs-{server.PDFJS_VERSION}/build/pdf.min.mjs'.encode(), page)
        self.assertNotIn(b'{{', page)
        version = server.web_values()['BOOK_VERSION']
        self.assertIn(f'/book.js?v={version}'.encode(), page)
        code, headers, js = self.req('/book.js')
        self.assertEqual(code, 200)
        self.assertTrue(headers['Content-Type'].startswith('text/javascript'))
        self.assertIn(b'export function splitChunks', js)
