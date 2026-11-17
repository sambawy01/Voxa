"""The agent API: keys, speech, audiobooks, podcasts and MCP, against a real server with the speech
model and the script writer stubbed out."""
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import server  # noqa: E402
from test_server import Keys, ServerTestCase, URL  # noqa: E402

import agentapi  # noqa: E402
import apikeys  # noqa: E402
import longjobs  # noqa: E402
import podcast  # noqa: E402

SCRIPT = {'title': 'Fake episode', 'summary': 'About things.', 'hosts': {'A': 'Maya', 'B': 'Leo'}, 'chapters': [
    {'title': 'Intro', 'lines': [{'speaker': 'A', 'text': 'Welcome to the show.'}, {'speaker': 'B', 'text': 'Glad to be here.'}]},
    {'title': 'Main', 'lines': [{'speaker': 'A', 'text': 'Let us dig in.'}, {'speaker': 'B', 'text': 'Absolutely.'}]}]}


def fake_mp3(pcm, rate):
    return b'\xff\xf3' + b'\0' * (len(pcm) // 40)


class AgentApiTest(ServerTestCase):
    """Sign-in on, smaller numbers: welcome credit 5,000 characters; Studio Standard 50,000 a month."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.jwt = Keys()

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        server._auth = server.TokenVerifier(URL, fetch_jwks=lambda: self.jwt.jwks)
        server._quota = server.Quota(':memory:', {'guest': 100})
        server._plans = server.plans.Plans(':memory:')
        server._keys = apikeys.ApiKeys(':memory:')
        server._longs = longjobs.LongJobs(self.tmp.name)
        server._rate = apikeys.RateLimit(1000, 60)
        self.saved = (server.plans.WELCOME_CHARS, server.plans.STANDARD_MONTHLY_CHARS, longjobs.encode_mp3,
                      longjobs.wav_to_mp3, podcast.DEEPSEEK_API_KEY, podcast.write_script)
        server.plans.WELCOME_CHARS, server.plans.STANDARD_MONTHLY_CHARS = 5000, 50000
        longjobs.encode_mp3, longjobs.wav_to_mp3 = fake_mp3, lambda wav: b'\xff\xf3MP3' + wav[:8]
        podcast.DEEPSEEK_API_KEY = 'test'
        self.scripts = []
        podcast.write_script = lambda req: self.scripts.append(req) or json.loads(json.dumps(SCRIPT))

    def tearDown(self):
        server._auth = server._quota = server._plans = server._keys = server._longs = None
        server._rate = apikeys.RateLimit(120, 60)
        (server.plans.WELCOME_CHARS, server.plans.STANDARD_MONTHLY_CHARS, longjobs.encode_mp3,
         longjobs.wav_to_mp3, podcast.DEEPSEEK_API_KEY, podcast.write_script) = self.saved
        self.tmp.cleanup()

    # helpers
    def call(self, method, path, body=None, token=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        r = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def js(self, *args, **kw):
        code, headers, body = self.call(*args, **kw)
        return code, json.loads(body) if body[:1] in (b'{', b'[') else body, headers

    def key_for(self, user, paid=False):
        server._keys.record_account(user, 'paid' if paid else 'free', f'{user}@example.com')
        return server._keys.create(user, 'test')[1]

    def finish(self, job_id, token, ok=('done',)):
        for _ in range(200):
            code, job, _ = self.js('GET', f'/api/v1/jobs/{job_id}', token=token)
            if job['status'] in ('done', 'error'):
                return job
            time.sleep(0.05)
        self.fail('job never finished')

    # --- keys ---
    def test_keys_are_made_in_a_session_shown_once_and_work_as_the_account(self):
        session = self.jwt.token(sub='alice', email='alice@example.com')
        code, made, _ = self.js('POST', '/api/keys', {'name': 'My agent'}, session)
        self.assertEqual(code, 201)
        secret = made['secret']
        self.assertTrue(secret.startswith('vx_live_'))
        code, listed, _ = self.js('GET', '/api/keys', token=session)
        self.assertEqual([k['name'] for k in listed['keys']], ['My agent'])
        self.assertNotIn(secret, json.dumps(listed))
        self.assertEqual(server._keys.tier('alice'), 'free')   # recorded when the session was used
        code, account, _ = self.js('GET', '/api/v1/account', token=secret)
        self.assertEqual((code, account['plan'], account['email']), (200, 'Voxa Lite', 'alice@example.com'))
        self.assertEqual(account['studio']['wallet'], 5000)
        # a key cannot make or list keys; only a session can
        self.assertEqual(self.js('POST', '/api/keys', {'name': 'x'}, secret)[0], 401)
        self.assertEqual(self.js('GET', '/api/keys', token=secret)[0], 401)
        # revoke
        self.assertEqual(self.js('DELETE', f'/api/keys/{made["key"]["id"]}', token=session)[0], 200)
        code, body, headers = self.js('GET', '/api/v1/account', token=secret)
        self.assertEqual(code, 401)
        self.assertIn('revoked', body['error'])
        self.assertEqual(self.js('DELETE', f'/api/keys/{made["key"]["id"]}', token=session)[0], 404)

    def test_five_active_keys_at_most_and_other_accounts_keys_are_not_yours(self):
        session = self.jwt.token(sub='bob')
        for i in range(5):
            self.assertEqual(self.js('POST', '/api/keys', {'name': f'k{i}'}, session)[0], 201)
        code, body, _ = self.js('POST', '/api/keys', {'name': 'one too many'}, session)
        self.assertEqual(code, 409)
        other = self.jwt.token(sub='carol')
        key_id = self.js('GET', '/api/keys', token=session)[1]['keys'][0]['id']
        self.assertEqual(self.js('DELETE', f'/api/keys/{key_id}', token=other)[0], 404)

    def test_tier_follows_the_account_and_an_unknown_key_is_refused(self):
        key = self.key_for('dan')
        self.assertEqual(self.js('GET', '/api/v1/account', token=key)[1]['plan'], 'Voxa Lite')
        server._keys.record_account('dan', 'paid')       # what the Paddle webhook does
        self.assertEqual(self.js('GET', '/api/v1/account', token=key)[1]['plan'], 'Voxa Studio')
        self.assertEqual(self.js('GET', '/api/v1/account', token='vx_live_nope')[0], 401)
        self.assertEqual(self.js('GET', '/api/v1/account')[0], 401)   # no credentials at all

    def test_calls_are_rate_limited_per_account(self):
        server._rate = apikeys.RateLimit(3, 60)
        key = self.key_for('erin')
        codes = [self.js('GET', '/api/v1/account', token=key)[0] for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])
        code, _, headers = self.js('GET', '/api/v1/account', token=key)
        self.assertGreaterEqual(int(headers['Retry-After']), 1)
        self.assertEqual(self.js('GET', '/api/v1/account', token=self.key_for('frank'))[0], 200)

    # --- speech ---
    def test_speech_returns_audio_and_is_charged_to_the_plan(self):
        key = self.key_for('gina')
        code, headers, body = self.call('POST', '/api/v1/speech', {'text': 'a' * 100}, key)
        self.assertEqual((code, headers['Content-Type']), (200, 'audio/mpeg'))
        self.assertTrue(body.startswith(b'\xff\xf3MP3'))
        self.assertEqual(headers['X-Voxa-Characters'], '100')
        code, headers, body = self.call('POST', '/api/v1/speech', {'text': 'hello', 'format': 'wav'}, key)
        self.assertEqual((code, body[:4]), (200, b'RIFF'))
        self.assertEqual(self.js('GET', '/api/v1/account', token=key)[1]['studio']['wallet'], 5000 - 105)
        self.assertEqual(self.js('POST', '/api/v1/speech', {'text': 'x', 'format': 'ogg'}, key)[0], 400)
        code, body, _ = self.js('POST', '/api/v1/speech', {'text': 'x' * 3000}, key)
        self.assertEqual(code, 200)
        code, body, _ = self.js('POST', '/api/v1/speech', {'text': 'y' * 3000}, key)   # wallet now too low
        self.assertEqual(code, 429)
        self.assertTrue(body['wallet'])

    # --- audiobooks ---
    def test_audiobook_job_runs_charges_and_gives_a_chaptered_mp3(self):
        key = self.key_for('hana')
        book = {'title': 'Tiny Tale', 'chapters': [{'title': 'One', 'text': 'First chapter words.'},
                                                   {'title': 'Two', 'text': 'Second chapter words.'}]}
        code, job, _ = self.js('POST', '/api/v1/audiobooks', book, key)
        self.assertEqual(code, 202)
        done = self.finish(job['id'], key)
        self.assertEqual(done['status'], 'done', done)
        self.assertEqual([c['title'] for c in done['chapters']], ['One', 'Two'])
        self.assertEqual(done['characters'], len('First chapter words.') + len('Second chapter words.'))
        code, headers, audio = self.call('GET', done['audio_url'].replace(self.base, ''))   # no key needed
        self.assertEqual((code, headers['Content-Type']), (200, 'audio/mpeg'))
        self.assertEqual(audio[:3], b'ID3')
        self.assertEqual(audio.count(b'CHAP'), 2)
        self.assertEqual(self.js('GET', '/api/v1/account', token=key)[1]['studio']['wallet'], 5000 - done['characters'])
        self.assertEqual(self.js('GET', f'/api/v1/jobs/{job["id"]}', token=self.key_for('ivan'))[0], 404)

    def test_audiobook_text_gets_chapters_from_headings_and_limits_apply(self):
        key = self.key_for('jo')
        text = 'Chapter 1\n\nIt began here.\n\nChapter 2\n\nIt went on.'
        job = self.js('POST', '/api/v1/audiobooks', {'text': text}, key)[1]
        done = self.finish(job['id'], key)
        self.assertEqual([c['title'] for c in done['chapters']], ['Chapter 1', 'Chapter 2'])
        # Voxa Lite: one new book a day
        code, body, _ = self.js('POST', '/api/v1/audiobooks', {'text': 'A different book entirely.'}, key)
        self.assertEqual(code, 429)
        self.assertIn('1 new book', body['error'])
        # Studio has no such limit
        paid = self.key_for('kim', paid=True)
        for i in range(2):
            job = self.js('POST', '/api/v1/audiobooks', {'text': f'Book number {i}.'}, paid)[1]
            self.assertEqual(self.finish(job['id'], paid)['status'], 'done')

    def test_audiobook_needs_funds_and_valid_input_and_failures_are_refunded(self):
        key = self.key_for('lee')
        code, body, _ = self.js('POST', '/api/v1/audiobooks', {'text': 'word ' * 1200}, key)   # 6,000 > 5,000
        self.assertEqual(code, 429)
        self.assertTrue(body['wallet'])
        for bad in ({}, {'text': ' '}, {'text': 'hi', 'voice': 'nobody'}, {'text': 'hi', 'lang': 'xx'}):
            self.assertEqual(self.js('POST', '/api/v1/audiobooks', bad, key)[0], 400, bad)
        self.gen.fail = True
        job = self.js('POST', '/api/v1/audiobooks', {'text': 'This will fail.'}, key)[1]
        done = self.finish(job['id'], key)
        self.assertEqual(done['status'], 'error')
        self.assertNotIn('secret', done['error'])
        self.assertEqual(self.js('GET', '/api/v1/account', token=key)[1]['studio']['wallet'], 5000)

    def test_unauthenticated_callers_get_a_clear_refusal(self):
        for path in ('/api/v1/audiobooks', '/api/v1/podcasts'):
            code, body, _ = self.js('POST', path, {'text': 'x ' * 40})
            self.assertEqual(code, 401, path)
        self.assertEqual(self.js('GET', '/api/v1/voices')[0], 200)   # the voice list is public

    # --- podcasts ---
    def test_podcast_writes_a_script_voices_it_and_returns_both(self):
        key = self.key_for('max', paid=True)
        code, job, _ = self.js('POST', '/api/v1/podcasts', {'text': 'word ' * 60, 'minutes': 5, 'title': 'My title'}, key)
        self.assertEqual(code, 202)
        done = self.finish(job['id'], key)
        self.assertEqual(done['status'], 'done', done)
        self.assertEqual((done['title'], done['script']['chapters'][0]['title']), ('My title', 'Intro'))
        self.assertEqual([c['title'] for c in done['chapters']], ['Intro', 'Main'])
        self.assertEqual(self.scripts[0]['minutes'], 5)
        voices = {(c.text, c.voice) for c in self.gen.calls if c.text in ('Welcome to the show.', 'Glad to be here.')}
        woman, man = server._default_voices('a')
        self.assertEqual(voices, {('Welcome to the show.', woman), ('Glad to be here.', man)})
        self.assertEqual(self.call('GET', done['audio_url'].replace(self.base, ''))[0], 200)

    def test_podcast_limits_and_funds_are_checked_before_any_script_is_written(self):
        key = self.key_for('ned')   # Lite: wallet 5,000 characters, 1 podcast a week up to 5 minutes
        text = 'word ' * 60
        code, body, _ = self.js('POST', '/api/v1/podcasts', {'text': text, 'minutes': 10}, key)
        self.assertEqual(code, 429)
        self.assertIn('up to 5 minutes', body['error'])
        server.plans.WELCOME_CHARS = 100
        poor = self.key_for('olly')
        code, body, _ = self.js('POST', '/api/v1/podcasts', {'text': text, 'minutes': 5}, poor)
        self.assertEqual(code, 429)
        self.assertTrue(body['wallet'])
        self.assertEqual(self.scripts, [])
        for bad in ({'text': 'too short'}, {'text': text, 'minutes': 7}, {'text': text, 'voices': {'a': 'nobody'}}):
            self.assertEqual(self.js('POST', '/api/v1/podcasts', bad, key)[0], 400, bad)

    def test_a_script_that_cannot_be_afforded_is_still_returned(self):
        # 2,300 passes the pre-check for a 5-minute episode (half of 4,500) but the script is longer than that.
        server.plans.WELCOME_CHARS = 2300
        key = self.key_for('quin')
        longer = json.loads(json.dumps(SCRIPT))
        longer['chapters'][0]['lines'][0]['text'] = 'x' * 2400
        podcast.write_script = lambda req: json.loads(json.dumps(longer))
        code, job, _ = self.js('POST', '/api/v1/podcasts', {'text': 'word ' * 60, 'minutes': 5}, key)
        self.assertEqual(code, 202)
        done = self.finish(job['id'], key)
        self.assertEqual(done['status'], 'done')
        self.assertNotIn('audio_url', done)
        self.assertEqual(done['script']['title'], 'Fake episode')
        self.assertIn('no audio was made', done['warning'])
        self.assertEqual(self.js('GET', '/api/v1/account', token=key)[1]['studio']['wallet'], 2300)   # nothing charged

    # --- cancel ---
    def test_a_running_job_can_be_cancelled_by_its_owner_only(self):
        key = self.key_for('ray')
        self.gen.gate.clear()
        job = self.js('POST', '/api/v1/audiobooks', {'chapters': [{'text': 'Slow words here.'}, {'text': 'More words.'}]}, key)[1]
        time.sleep(0.2)
        self.assertEqual(self.js('DELETE', f'/api/v1/jobs/{job["id"]}', token=self.key_for('sam'))[0], 404)
        self.assertEqual(self.js('DELETE', f'/api/v1/jobs/{job["id"]}', token=key)[0], 200)
        self.gen.gate.set()
        done = self.finish(job['id'], key)
        self.assertEqual((done['status'], done['error']), ('error', 'Cancelled.'))

    # --- docs ---
    def test_openapi_and_llms_txt_describe_every_operation(self):
        code, spec, _ = self.js('GET', '/api/openapi.json')
        self.assertEqual((code, spec['openapi']), (200, '3.1.0'))
        ids = {op['operationId'] for item in spec['paths'].values() for op in item.values()}
        self.assertEqual(ids, set(agentapi.OPERATIONS) | {'download_file'})
        self.assertIn('/api/v1/audiobooks', spec['paths'])
        code, text, headers = self.js('GET', '/llms.txt')
        self.assertTrue(headers['Content-Type'].startswith('text/plain'))
        self.assertIn('/api/v1/podcasts', text.decode())
        self.assertEqual(self.call('GET', '/developers')[0], 200)

    # --- MCP ---
    def rpc(self, message, token=None):
        return self.js('POST', '/mcp', message, token)

    def test_mcp_handshake_lists_tools_and_needs_a_key_to_call_them(self):
        code, reply, _ = self.rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}})
        self.assertEqual((code, reply['result']['protocolVersion'], reply['result']['serverInfo']['name']), (200, '2025-06-18', 'Voxa'))
        self.assertEqual(self.call('POST', '/mcp', {'jsonrpc': '2.0', 'method': 'notifications/initialized'})[0], 202)
        code, reply, _ = self.rpc({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        self.assertEqual({t['name'] for t in reply['result']['tools']}, set(agentapi.OPERATIONS))
        code, body, headers = self.rpc({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'get_account'}})
        self.assertEqual((code, headers['WWW-Authenticate']), (401, 'Bearer'))
        self.assertEqual(self.rpc({'jsonrpc': '2.0', 'id': 4, 'method': 'nope'})[1]['error']['code'], -32601)
        self.assertEqual(self.call('GET', '/mcp')[0], 405)

    def test_mcp_tools_do_the_work(self):
        key = self.key_for('tom')

        def tool(name, args, rid=1):
            code, reply, _ = self.rpc({'jsonrpc': '2.0', 'id': rid, 'method': 'tools/call',
                                       'params': {'name': name, 'arguments': args}}, key)
            self.assertEqual(code, 200, reply)
            return reply['result']
        self.assertEqual(tool('get_account', {})['structuredContent']['plan'], 'Voxa Lite')
        voices = tool('list_voices', {'lang': 'a'})['structuredContent']['voices']
        self.assertIn('harper', [v['id'] for v in voices])
        spoken = tool('speak', {'text': 'Hello from an agent.'})['structuredContent']
        self.assertEqual(self.call('GET', spoken['audio_url'].replace(self.base, ''))[0], 200)
        started = tool('make_audiobook', {'title': 'T', 'text': 'Some words to read.'})['structuredContent']
        done = tool('get_job', {'id': started['id'], 'wait_seconds': 5})['structuredContent']
        for _ in range(100):
            if done['status'] == 'done':
                break
            done = tool('get_job', {'id': started['id'], 'wait_seconds': 2})['structuredContent']
        self.assertEqual(done['status'], 'done')
        # errors come back as tool errors the agent can read, not as protocol failures
        bad = tool('speak', {'text': ''})
        self.assertTrue(bad['isError'])
        self.assertIn('empty', bad['content'][0]['text'])
        missing = tool('get_job', {'id': 'nope'})
        self.assertTrue(missing['isError'])
        code, reply, _ = self.rpc([{'jsonrpc': '2.0', 'id': 7, 'method': 'ping'}, {'jsonrpc': '2.0', 'method': 'notifications/x'}], key)
        self.assertEqual((code, len(reply), reply[0]['id']), (200, 1, 7))


if __name__ == '__main__':
    unittest.main()
