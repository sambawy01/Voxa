"""The API for AI agents (agentapi.py): REST /v1 and MCP /mcp. Speech is stubbed, so these
run without onnxruntime or the models.
    python -m unittest discover -s tests
"""
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import agentapi  # noqa: E402
import server  # noqa: E402

KEY = 'vx_test_key_0123456789'


class FakeVoices:
    """Two voices; each line becomes a short tone, and every call is recorded."""
    def __init__(self):
        self.by_id = {'lina-a': {'id': 'lina-a', 'name': 'Lina', 'gender': 'female', 'language': 'a',
                                 'language_name': 'English (US)', 'engine': 'supertonic'},
                      'adam-a': {'id': 'adam-a', 'name': 'Adam', 'gender': 'male', 'language': 'a',
                                 'language_name': 'English (US)', 'engine': 'supertonic'}}
        self.calls = []

    def languages(self):
        return [{'code': 'a', 'name': 'English (US)', 'voices': 2}]

    def list(self, language=None, engine=None, gender=None):
        return [v for v in self.by_id.values() if not gender or v['gender'] == gender]

    def get(self, voice_id):
        if voice_id not in self.by_id:
            raise agentapi.ApiError(400, f'Unknown voice {voice_id!r}.')
        return self.by_id[voice_id]

    def synth(self, voice_id, text, seed=4242):
        import numpy as np
        self.get(voice_id)
        self.calls.append((voice_id, text))
        return (0.1 * np.sin(np.arange(2400) / 10)).astype(np.float32)


def make_api(daily=100_000):
    d = tempfile.mkdtemp()
    keys = agentapi.Keys(f'owner:{hashlib.sha256(KEY.encode()).hexdigest()}', d, daily)
    return agentapi.Api(server, data_dir=d, keys=keys, voices=FakeVoices(), secret='s3cret')


def wait(api, job_id, owner='owner'):
    for _ in range(1000):   # up to 20 s, for a busy machine
        job = api.get_job(owner, 'http://x', job_id)
        if job['status'] in ('done', 'error', 'cancelled'):
            return job
        time.sleep(0.02)
    raise AssertionError('job never finished')


SCRIPT = """Sure! Here's your script:
# Coffee Talk
## Opening
**Maya:** Welcome to Coffee Talk!
Leo: Thanks, Maya.
[laughs]
## The beans
Maya: So, beans. What changed?
Leo: Everything. Climate,
supply, price."""


class ScriptTest(unittest.TestCase):
    def test_script_parsing(self):
        title, names, chapters = agentapi.parse_script(SCRIPT)
        self.assertEqual(title, 'Coffee Talk')
        self.assertEqual(names, ['Maya', 'Leo'])
        self.assertEqual([c['title'] for c in chapters], ['Opening', 'The beans'])
        self.assertEqual(chapters[1]['lines'][1], {'speaker': 'Leo', 'text': 'Everything. Climate, supply, price.'})
        with self.assertRaises(agentapi.ApiError):
            agentapi.parse_script('Just a paragraph.')

    def test_book_chapters_and_paragraphs(self):
        chs = agentapi.chapters_from_text('Preface text.\n\nChapter 1\nIt begins.\n\nChapter 2: Later\nIt ends.')
        self.assertEqual([c['title'] for c in chs], ['Opening', 'Chapter 1', 'Chapter 2: Later'])
        self.assertEqual(agentapi.chapters_from_text('No headings at all.')[0]['title'], 'Chapter 1')
        parts = agentapi.split_paragraphs('A. ' * 1500, mx=500)
        self.assertTrue(all(len(p) <= 500 for p in parts) and len(parts) > 1)

    def test_resample_keeps_duration(self):
        import numpy as np
        x = np.sin(np.arange(44100) / 20).astype(np.float32)
        self.assertEqual(len(agentapi.resample(x, 44100, 24000)), 24000)


class KeysTest(unittest.TestCase):
    def test_only_the_hash_is_kept_and_the_allowance_holds(self):
        api = make_api(daily=100)
        self.assertEqual(api.keys.check(KEY), 'owner')
        self.assertIsNone(api.keys.check('wrong'))
        self.assertNotIn(KEY, json.dumps(api.keys.by_hash))
        refund = api.keys.charge('owner', 80)
        with self.assertRaises(agentapi.ApiError) as cm:
            api.keys.charge('owner', 30)
        self.assertEqual(cm.exception.status, 429)
        refund()
        api.keys.charge('owner', 30)   # the refund freed the room
        self.assertEqual(api.keys.used('owner'), 30)
        with self.assertRaises(ValueError):
            agentapi.Keys('bad-entry', tempfile.mkdtemp(), 0)


class JobsTest(unittest.TestCase):
    def test_podcast_job_links_and_one_line_edit(self):
        api = make_api()
        job = api.create_podcast('owner', 'http://x', script=SCRIPT, voices={'Maya': 'lina-a', 'Leo': 'adam-a'})
        self.assertEqual(job['title'], 'Coffee Talk')
        done = wait(api, job['id'])
        self.assertEqual(done['status'], 'done')
        self.assertEqual([c['title'] for c in done['chapters']], ['Opening', 'The beans'])
        self.assertEqual(done['chapters'][0]['start'], 0)
        link = done['audio']['wav']
        token = link.split('token=')[1]
        data, name = api.audio(job['id'], 'wav', token)
        self.assertTrue(data.startswith(b'RIFF'))
        self.assertEqual(name, 'Coffee Talk')
        with self.assertRaises(agentapi.ApiError):
            api.audio(job['id'], 'wav', 'forged')
        # editing one line makes only that line again
        calls = len(api.voices.calls)
        api.edit_line('owner', 'http://x', job['id'], 1, text='Thanks, Maya! Great to be here.', voice='lina-a')
        self.assertEqual(wait(api, job['id'])['status'], 'done')
        self.assertEqual(api.voices.calls[calls:], [('lina-a', 'Thanks, Maya! Great to be here.')])
        script = api.get_job('owner', 'http://x', job['id'], script=True)['script']
        self.assertEqual((script[1]['voice'], script[1]['speaker']), ('lina-a', 'Leo'))

    def test_jobs_belong_to_their_key_and_need_voices(self):
        api = make_api()
        with self.assertRaises(agentapi.ApiError) as cm:
            api.create_podcast('owner', 'http://x', script=SCRIPT, voices={'Maya': 'lina-a'})
        self.assertIn('Leo', cm.exception.message)
        with self.assertRaises(agentapi.ApiError):
            api.create_podcast('owner', 'http://x', script=SCRIPT, voices={'Maya': 'lina-a', 'Leo': 'nope'})
        job = api.create_audiobook('owner', 'http://x', voice='adam-a', text='Chapter 1\nHello.\n\nChapter 2\nBye.')
        with self.assertRaises(agentapi.ApiError) as cm:
            api.get_job('someone-else', 'http://x', job['id'])
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(wait(api, job['id'])['status'], 'done')
        self.assertEqual(len(api.list_jobs('owner', 'http://x')['jobs']), 1)

    def test_jobs_survive_a_restart(self):
        api = make_api()
        job = api.create_podcast('owner', 'http://x', lines=[{'speaker': 'A', 'text': 'Hi.'}, {'speaker': 'B', 'text': 'Hey.'}],
                                 voices={'A': 'lina-a', 'B': 'adam-a'})
        wait(api, job['id'])
        again = agentapi.Api(server, data_dir=api.data_dir, keys=api.keys, voices=FakeVoices(), secret='s3cret')
        self.assertEqual(again.get_job('owner', 'http://x', job['id'])['status'], 'done')


class McpTest(unittest.TestCase):
    def call(self, api, method, params=None, key=KEY, mid=1):
        return api.mcp({'jsonrpc': '2.0', 'id': mid, 'method': method, 'params': params or {}}, key, 'http://x')

    def test_handshake_tools_and_calls(self):
        api = make_api()
        init = self.call(api, 'initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                             'clientInfo': {'name': 't', 'version': '1'}})['result']
        self.assertEqual(init['protocolVersion'], '2025-06-18')
        self.assertIn('tools', init['capabilities'])
        self.assertIsNone(api.mcp({'jsonrpc': '2.0', 'method': 'notifications/initialized'}, KEY, 'http://x'))
        names = [t['name'] for t in self.call(api, 'tools/list')['result']['tools']]
        self.assertEqual(names, ['list_device_voices', 'play_on_device', 'list_languages', 'list_voices', 'make_speech', 'create_podcast', 'create_audiobook',
                                 'get_job', 'list_jobs', 'edit_line', 'cancel_job'])
        res = self.call(api, 'tools/call', {'name': 'create_podcast', 'arguments': {
            'script': SCRIPT, 'voices': {'Maya': 'lina-a', 'Leo': 'adam-a'}}})['result']
        self.assertFalse(res.get('isError'))
        job_id = res['structuredContent']['id']
        self.assertEqual(wait(api, job_id)['status'], 'done')
        got = self.call(api, 'tools/call', {'name': 'get_job', 'arguments': {'job_id': job_id}})['result']['structuredContent']
        self.assertIn('/v1/jobs/' + job_id + '/audio.wav?token=', got['audio']['wav'])
        # errors come back as tool errors the agent can read, not protocol failures
        bad = self.call(api, 'tools/call', {'name': 'list_voices', 'arguments': {}}, key='wrong')['result']
        self.assertTrue(bad['isError'])
        bad = self.call(api, 'tools/call', {'name': 'edit_line', 'arguments': {'job_id': job_id, 'line': 99, 'text': 'x'}})['result']
        self.assertTrue(bad['isError'])
        self.assertEqual(self.call(api, 'nope')['error']['code'], -32601)


class DeviceTest(unittest.TestCase):
    """play_on_device and list_device_voices: free and keyless, the audio is made in the user's browser."""
    def call(self, api, method, params=None, key=None):
        return api.mcp({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}, key, 'https://voxa.test')

    def tool(self, api, name, **args):
        return self.call(api, 'tools/call', {'name': name, 'arguments': args})['result']

    def test_listed_with_the_player(self):
        api = make_api()
        init = self.call(api, 'initialize', {'protocolVersion': '2025-06-18'})['result']
        self.assertIn('io.modelcontextprotocol/ui', init['capabilities']['extensions'])
        tools = {t['name']: t for t in self.call(api, 'tools/list')['result']['tools']}
        self.assertEqual(tools['play_on_device']['_meta']['ui']['resourceUri'], agentapi.PLAYER_URI)
        res = self.call(api, 'resources/read', {'uri': agentapi.PLAYER_URI})['result']['contents'][0]
        self.assertEqual(res['mimeType'], 'text/html;profile=mcp-app')
        self.assertEqual(res['_meta']['ui']['csp']['frameDomains'], ['https://voxa.test'])
        self.assertIn('"https://voxa.test"', res['text'])
        self.assertIn('ui/initialize', res['text'])
        self.assertEqual(self.call(api, 'resources/read', {'uri': 'ui://nope'})['error']['code'], -32002)
        listed = self.call(api, 'resources/list')['result']['resources']
        self.assertEqual([r['uri'] for r in listed], [agentapi.PLAYER_URI])

    def test_play_a_script_without_a_key(self):
        api = make_api()
        r = self.tool(api, 'play_on_device', script=SCRIPT, voices={'Leo': 'calvin'}, style='expressive')
        self.assertNotIn('isError', r)
        d = r['structuredContent']
        self.assertEqual(d['title'], 'Coffee Talk')
        self.assertEqual({s['name']: s['voice'] for s in d['speakers']}, {'Maya': 'wren', 'Leo': 'calvin'})
        self.assertEqual(d['temperature'], 0.5)
        self.assertEqual([l['speaker'] for l in d['lines']][:2], ['Maya', 'Leo'])
        self.assertEqual(d['chapters'], ['Opening', 'The beans'])
        self.assertIn('https://voxa.test/player#d=', r['content'][0]['text'])
        self.assertEqual(api.voices.calls, [])           # nothing made on the server

    def test_read_text_in_another_language(self):
        api = make_api()
        d = self.tool(api, 'play_on_device', text='Bonjour à tous.\n\nDeuxième paragraphe.', language='f')['structuredContent']
        self.assertEqual(len(d['speakers']), 1)
        self.assertTrue(d['speakers'][0]['voice'].endswith('-f'))
        self.assertEqual(len(d['lines']), 2)

    def test_mistakes(self):
        api = make_api()
        self.assertTrue(self.tool(api, 'play_on_device', script='A: hi', voices={'A': 'nope'})['isError'])
        self.assertTrue(self.tool(api, 'play_on_device')['isError'])
        self.assertTrue(self.tool(api, 'play_on_device', script='A: hi', style='loud')['isError'])
        long = 'A: ' + 'word ' * 400 + '\n'
        self.assertIn('60,000', self.tool(api, 'play_on_device', script=long * 40)['content'][0]['text'])
        self.assertTrue(self.tool(api, 'list_jobs')['isError'])   # the server's own tools still need a key

    def test_voice_list(self):
        api = make_api()
        langs = self.tool(api, 'list_device_voices')['structuredContent']['languages']
        self.assertIn('ar', [l['code'] for l in langs])
        voices = self.tool(api, 'list_device_voices', language='a', gender='male')['structuredContent']['voices']
        self.assertTrue(voices and all(v['gender'] == 'male' and v['language'] == 'a' for v in voices))
        self.assertIn('calvin', [v['id'] for v in voices])


class HttpTest(unittest.TestCase):
    """The routes through the real server, which hands /v1 and /mcp to the agent API."""

    @classmethod
    def setUpClass(cls):
        server._agent_api = make_api()
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.base = f'http://127.0.0.1:{cls.httpd.server_address[1]}'
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        server._agent_api = None

    def req(self, path, body=None, key=KEY, method=None):
        headers = {'Content-Type': 'application/json'}
        if key:
            headers['Authorization'] = 'Bearer ' + key
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_rest_flow(self):
        self.assertEqual(self.req('/v1/voices', key=None)[0], 401)
        code, _, body = self.req('/v1/voices')
        self.assertEqual(code, 200)
        self.assertEqual(len(json.loads(body)['voices']), 2)
        code, _, body = self.req('/v1/podcasts', {'script': SCRIPT, 'voices': {'Maya': 'lina-a', 'Leo': 'adam-a'}})
        self.assertEqual(code, 202)
        job_id = json.loads(body)['id']
        for _ in range(1000):
            job = json.loads(self.req(f'/v1/jobs/{job_id}')[2])
            if job['status'] == 'done':
                break
            time.sleep(0.02)
        code, headers, audio = self.req(job['audio']['wav'][job['audio']['wav'].index('/v1/'):], key=None)
        self.assertEqual(code, 200)               # the signed link needs no key
        self.assertTrue(audio.startswith(b'RIFF'))
        self.assertIn('Coffee Talk.wav', headers['Content-Disposition'])
        code, _, body = self.req(f'/v1/jobs/{job_id}/lines/0', {'text': 'Hello again!'}, method='PATCH')
        self.assertEqual(code, 202)
        self.assertEqual(self.req(f'/v1/jobs/{job_id}', method='DELETE')[0], 200)
        self.assertEqual(self.req('/v1/podcasts', {'script': SCRIPT})[0], 400)
        self.assertEqual(self.req('/v1/nothing')[0], 404)

    def test_mcp_over_http(self):
        code, _, body = self.req('/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                          'params': {'protocolVersion': '2025-06-18'}}, key=None)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['result']['serverInfo']['name'], 'voxa')
        code, _, _ = self.req('/mcp', {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, key=None)
        self.assertEqual(code, 202)
        # the key can also come in the URL, for clients that cannot set headers
        code, _, body = self.req('/mcp?key=' + KEY, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                                                    'params': {'name': 'list_languages', 'arguments': {}}}, key=None)
        self.assertEqual(json.loads(body)['result']['structuredContent']['languages'][0]['code'], 'a')
        self.assertEqual(self.req('/mcp', key=None)[0], 405)


if __name__ == '__main__':
    unittest.main()
