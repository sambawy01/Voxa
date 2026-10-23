"""Podcast scripts: request checks, the model call against a fake DeepSeek, and the HTTP flow."""
import json
import os
import sys
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import podcast  # noqa: E402
import server  # noqa: E402
import test_server as ts  # noqa: E402

DOC = ' '.join(['The solar co-op sells shares to neighbours and pays dividends from power sales.'] * 10)


def script(chapters=2, lines=3):
    return {'title': 'The **co-op** story', 'summary': 'How it works.',
            'chapters': [{'title': f'Part {c}', 'lines': [
                {'speaker': 'AB'[i % 2], 'text': f'Line {i} of part {c}.'} for i in range(lines)]}
                for c in range(chapters)]}


class ParseRequestTest(unittest.TestCase):
    def test_defaults(self):
        req = podcast.parse_request({'text': DOC})
        self.assertEqual((req['minutes'], req['tone'], req['lang'], req['hosts']), (10, 'friendly', 'a', ['Maya', 'Leo']))
        self.assertEqual(req['genders'], ['a woman', 'a man'])

    def test_rejects_bad_input(self):
        cases = [({'text': 'too short'}, 'some text'),
                 ({'text': 'x ' * podcast.MAX_SOURCE}, 'too long'),
                 ({'text': DOC, 'minutes': 7}, 'minutes'),
                 ({'text': DOC, 'minutes': 'ten'}, 'minutes'),
                 ({'text': DOC, 'tone': 'angry'}, 'tone'),
                 ({'text': DOC, 'lang': 'xx'}, 'language'),
                 ({'text': DOC, 'focus': 'f' * 1001}, 'focus'),
                 ({'text': DOC, 'hosts': {'a': 'Sam', 'b': 'sam'}}, 'different names'),
                 ({'text': DOC, 'hosts': {'a': ''}}, 'host names'),
                 ([1, 2], 'JSON object')]
        for body, fragment in cases:
            with self.subTest(fragment=fragment):
                with self.assertRaisesRegex(podcast.BadRequest, fragment):
                    podcast.parse_request(body)

    def test_prompt_carries_focus_language_and_hosts(self):
        req = podcast.parse_request({'text': DOC, 'focus': 'the risks', 'lang': 'e', 'minutes': 5,
                                     'hosts': {'a': 'Lucía', 'b': 'Mateo', 'a_gender': 'woman', 'b_gender': 'man'}})
        system, user = (m['content'] for m in podcast.build_messages(req))
        for needle in ('Lucía, a woman', 'Mateo, a man', 'Spanish', '750 words', 'json'):
            self.assertIn(needle, system)
        self.assertIn('the risks', user)
        self.assertIn(DOC, user)


class CheckScriptTest(unittest.TestCase):
    def test_cleans_lines_for_speech(self):
        raw = script()
        raw['chapters'][0]['lines'][0]['text'] = '[laughs] **Really?** (chuckles) See https://x.io *sighs* yes'
        raw['chapters'][0]['lines'].append({'speaker': 'c', 'text': 'nobody'})
        raw['chapters'][0]['lines'].append({'speaker': 'b', 'text': '   '})
        out = podcast.check_script(raw, ['Maya', 'Leo'])
        self.assertEqual(out['title'], 'The co-op story')
        self.assertEqual(out['hosts'], {'A': 'Maya', 'B': 'Leo'})
        self.assertEqual(out['chapters'][0]['lines'][0]['text'], 'Really? See yes')
        self.assertEqual(len(out['chapters'][0]['lines']), 3)

    def test_rejects_empty_or_wrong_shapes(self):
        for raw in ({}, {'chapters': 'x'}, {'chapters': [{'lines': [{'speaker': 'A', 'text': 'hi'}]}]}, []):
            with self.subTest(raw=raw), self.assertRaises(podcast.ModelError):
                podcast.check_script(raw, ['Maya', 'Leo'])

    def test_write_script_retries_once(self):
        replies = [podcast.ModelError('empty'), script()]

        def call(messages, max_tokens):
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        out = podcast.write_script(podcast.parse_request({'text': DOC}), call)
        self.assertEqual(len(out['chapters']), 2)
        with self.assertRaises(podcast.ModelError):
            podcast.write_script(podcast.parse_request({'text': DOC}), lambda m, t: {})


class FakeDeepSeek(BaseHTTPRequestHandler):
    reply = None   # (status, body) for the next request
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        FakeDeepSeek.seen.append((self.path, self.headers.get('Authorization'), body))
        status, out = FakeDeepSeek.reply
        data = json.dumps(out).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class CallModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), FakeDeepSeek)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.saved = podcast.DEEPSEEK_API, podcast.DEEPSEEK_API_KEY
        podcast.DEEPSEEK_API = f'http://127.0.0.1:{cls.httpd.server_address[1]}'
        podcast.DEEPSEEK_API_KEY = 'sk-test'

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        podcast.DEEPSEEK_API, podcast.DEEPSEEK_API_KEY = cls.saved

    def test_sends_json_mode_request_and_parses_content(self):
        FakeDeepSeek.seen = []
        FakeDeepSeek.reply = (200, {'choices': [{'message': {'content': json.dumps(script())}, 'finish_reason': 'stop'}],
                                    'usage': {'prompt_tokens': 10, 'completion_tokens': 20}})
        out = podcast.call_model([{'role': 'user', 'content': 'json'}], 500)
        self.assertEqual(out['title'], 'The **co-op** story')
        path, auth, body = FakeDeepSeek.seen[0]
        self.assertEqual((path, auth), ('/chat/completions', 'Bearer sk-test'))
        self.assertEqual(body['model'], podcast.MODEL)
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertEqual(body['thinking'], {'type': 'disabled'})
        self.assertEqual(body['max_tokens'], 500)

    def test_errors_become_friendly_messages(self):
        for status, out, fragment in ((402, {'error': {}}, 'out of credit'), (429, {}, 'busy'),
                                      (500, {}, 'failed'), (200, {'choices': [{'message': {'content': ''}}]}, 'incomplete'),
                                      (200, {'choices': [{'message': {'content': '{"cut'}}]}, 'incomplete')):
            with self.subTest(status=status, fragment=fragment):
                FakeDeepSeek.reply = (status, out)
                with self.assertRaisesRegex(podcast.ModelError, fragment):
                    podcast.call_model([], 10)


class ScriptJobsTest(unittest.TestCase):
    def wait(self, job):
        self.assertTrue(job.done.wait(5))
        return job

    def test_runs_refunds_on_failure_and_hides_other_peoples_jobs(self):
        refunds = []
        jobs = podcast.ScriptJobs(writer=lambda req: {'title': req['text']})
        job = self.wait(jobs.submit({'text': 'ok'}, 'u:1', lambda: refunds.append(1)))
        self.assertEqual(job.to_json(), {'id': job.id, 'status': 'done', 'script': {'title': 'ok'}})
        self.assertIsNone(job.req)  # the document is dropped once written
        self.assertIsNone(jobs.get(job.id, 'u:2'))
        self.assertIs(jobs.get(job.id, 'u:1'), job)
        self.assertEqual(refunds, [])

        def boom(req):
            raise podcast.ModelError('nope')
        bad = self.wait(podcast.ScriptJobs(writer=boom).submit({}, 'u:1', lambda: refunds.append(1)))
        self.assertEqual((bad.status, bad.error, refunds), ('error', 'nope', [1]))

    def test_limits_scripts_written_at_once(self):
        gate = threading.Event()
        jobs = podcast.ScriptJobs(writer=lambda req: gate.wait(5), max_running=1)
        jobs.submit({}, 'u:1')
        with self.assertRaises(podcast.Busy):
            jobs.submit({}, 'u:2')
        gate.set()


class PodcastHTTPTest(ts.ServerTestCase):
    """Sign-in on: guests none, free accounts 1 a day, paid 2 (smaller numbers for the test)."""

    def setUp(self):
        super().setUp()
        self.keys = getattr(PodcastHTTPTest, '_keys', None) or ts.Keys()
        PodcastHTTPTest._keys = self.keys
        self.saved = podcast.DEEPSEEK_API_KEY, server.PODCAST_DAILY, server._scripts
        podcast.DEEPSEEK_API_KEY = 'sk-test'
        server.PODCAST_DAILY = {'guest': 0, 'free': 1, 'paid': 2}
        server._auth = server.TokenVerifier(ts.URL, fetch_jwks=lambda: self.keys.jwks)
        server._podcast_quota = server.Quota(':memory:', server.podcast_limits(), 'podcast_usage')
        self.written = []
        server._scripts = podcast.ScriptJobs(writer=lambda req: self.written.append(req) or podcast.check_script(script(), req['hosts']))

    def tearDown(self):
        podcast.DEEPSEEK_API_KEY, server.PODCAST_DAILY, server._scripts = self.saved
        server._auth = None
        server._podcast_quota = None

    def call(self, path, body=None, token=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        r = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def finish(self, job_id, token):
        for _ in range(100):
            code, job = self.call(f'/api/podcast/script/{job_id}', token=token)
            if job.get('status') != 'running':
                return code, job
            time.sleep(0.02)
        self.fail('script never finished')

    def test_free_account_writes_one_script_a_day(self):
        token = self.keys.token(sub='alice')
        code, job = self.call('/api/podcast/script', {'text': DOC, 'focus': 'money'}, token)
        self.assertEqual(code, 202)
        code, job = self.finish(job['id'], token)
        self.assertEqual((code, job['status']), (200, 'done'))
        self.assertEqual(job['script']['hosts'], {'A': 'Maya', 'B': 'Leo'})
        self.assertEqual(self.written[0]['focus'], 'money')
        # Someone else cannot read it.
        code, _ = self.call(f'/api/podcast/script/{job["id"]}', token=self.keys.token(sub='mallory'))
        self.assertEqual(code, 404)
        code, err = self.call('/api/podcast/script', {'text': DOC}, token)
        self.assertEqual(code, 429)
        self.assertIn('1 podcast script for today', err['error'])

    def test_failed_script_is_refunded(self):
        def boom(req):
            raise podcast.ModelError('The script writer is busy.')
        server._scripts = podcast.ScriptJobs(writer=boom)
        token = self.keys.token(sub='carol')
        _, job = self.call('/api/podcast/script', {'text': DOC}, token)
        _, job = self.finish(job['id'], token)
        self.assertEqual(job, {'id': job['id'], 'status': 'error', 'error': 'The script writer is busy.'})
        server._scripts = podcast.ScriptJobs(writer=lambda req: podcast.check_script(script(), req['hosts']))
        code, _ = self.call('/api/podcast/script', {'text': DOC}, token)
        self.assertEqual(code, 202)

    def test_guests_must_sign_in_and_bad_requests_are_400(self):
        code, err = self.call('/api/podcast/script', {'text': DOC})
        self.assertEqual((code, err['error']), (403, 'Sign in to make podcasts.'))
        code, err = self.call('/api/podcast/script', {'text': 'short'}, self.keys.token(sub='dave'))
        self.assertEqual(code, 400)
        self.assertIn('some text', err['error'])
        self.assertEqual(self.written, [])

    def test_paid_accounts_get_more(self):
        token = self.keys.token(sub='erin', app_metadata={'tier': 'paid'})
        for expected in (202, 202, 429):
            code, _ = self.call('/api/podcast/script', {'text': DOC}, token)
            self.assertEqual(code, expected)

    def test_config_and_page(self):
        _, config = self.call('/api/config')
        self.assertEqual(config['podcast']['daily'], {'guest': 0, 'free': 1, 'paid': 2})
        self.assertEqual(config['podcast']['minutes'], [5, 10, 20])
        code, headers, page = self.req('/podcast')
        self.assertEqual(code, 200)
        self.assertEqual(headers['Cross-Origin-Embedder-Policy'], 'require-corp')
        self.assertNotIn(b'{{', page)
        self.assertIn(f'/podcast.js?v={server.web_values()["PODCAST_VERSION"]}'.encode(), page)
        self.assertIn(b'DeepSeek', page)
        code, _, js = self.req('/podcast.js')
        self.assertIn(b'export async function pptxText', js)
        _, _, home = self.req('/')
        self.assertIn(b'href="/podcast"', home)


class PodcastOffTest(ts.ServerTestCase):
    def test_everything_is_404_without_a_key(self):
        self.assertEqual(podcast.DEEPSEEK_API_KEY, '')
        _, _, body = self.req('/api/config')
        self.assertIsNone(json.loads(body)['podcast'])
        code, _, _ = self.req('/api/podcast/script', {'text': DOC})
        self.assertEqual(code, 404)
        code, _, _ = self.req('/api/podcast/script/abc')
        self.assertEqual(code, 404)

    def test_guests_get_the_free_allowance_until_sign_in_exists(self):
        self.assertIsNone(server._auth)
        self.assertEqual(server.podcast_limits()['guest'], server.PODCAST_DAILY['free'])


if __name__ == '__main__':
    unittest.main()


class PreviewTest(ts.ServerTestCase):
    """Voice previews: a fixed line per language, generated once, kept on disk, never charged."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = server.DATA_DIR
        server.DATA_DIR = self.tmp.name
        server._quota = server.Quota(':memory:', {'guest': 10, 'free': 10, 'paid': 0})

    def tearDown(self):
        server.DATA_DIR = self.saved
        server._quota = None
        self.tmp.cleanup()

    def test_every_language_has_a_line_and_latin_names_are_used(self):
        self.assertEqual(set(server.PREVIEW_LINES), set(server.LANG_LABELS))
        self.assertIn('Harper', server.preview_text('harper'))
        for vid in server.VOICE_BY_ID:
            self.assertNotIn('{', server.preview_text(vid))

    def test_generated_once_then_served_from_disk_without_charge(self):
        calls = len(self.gen.calls)
        code, headers, body = self.req('/api/preview/harper')
        self.assertEqual((code, headers['Content-Type']), (200, 'audio/wav'))
        self.assertTrue(body.startswith(b'RIFF'))
        self.assertEqual(len(self.gen.calls), calls + 1)
        self.assertEqual(self.gen.calls[-1].text, server.preview_text('harper'))
        files = os.listdir(os.path.join(self.tmp.name, 'previews'))
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].startswith('harper-'))
        server._cache = server.AudioCache(server.MAX_CACHE_BYTES)  # as after a restart
        code, _, again = self.req('/api/preview/harper')
        self.assertEqual((code, again), (200, body))
        self.assertEqual(len(self.gen.calls), calls + 1)
        # The guest allowance (10 characters) is untouched.
        _, _, quota = self.req('/api/quota')
        self.assertEqual(json.loads(quota)['quota']['remaining'], 10)

    def test_unknown_voice_and_failure(self):
        self.assertEqual(self.req('/api/preview/nobody')[0], 404)
        self.assertEqual(self.req('/api/preview/..%2Fetc')[0], 404)
        self.gen.fail = True
        code, _, _ = self.req('/api/preview/sienna')
        self.assertEqual(code, 503)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, 'previews')))
