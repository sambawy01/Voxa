"""The GPU service and the routing of paid jobs to it. Synthesis is stubbed; the HTTP,
token checks, validation and routing are real.

    python -m unittest discover -s tests
"""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'gpu'))
import server  # noqa: E402
import service  # noqa: E402
from test_server import FakeGenerator, ServerTestCase, make_wav  # noqa: E402


def post(url, body, token=None, raw=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    req = urllib.request.Request(url, data=raw if raw is not None else json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class FakeGPU:
    """The real service handler on a free port, with synthesis replaced by a counter."""

    def __init__(self, token='gpu-secret'):
        self.calls = []
        handler = type('H', (service.Handler,), {'token': token})
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.httpd.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        self._orig = service.synthesize

        def fake(body):
            text, lang, voice, speed = server.parse_request(body)  # real validation
            self.calls.append((text, lang, voice))
            return make_wav(0.2)
        service.synthesize = fake
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        service.synthesize = self._orig
        self.httpd.shutdown()


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.gpu = FakeGPU()

    def tearDown(self):
        self.gpu.close()

    def test_token_is_required(self):
        url = self.gpu.url + '/synthesize'
        self.assertEqual(post(url, {'text': 'hi'})[0], 401)
        self.assertEqual(post(url, {'text': 'hi'}, token='wrong')[0], 401)
        code, audio = post(url, {'text': 'hi', 'voice': 'aurora'}, token='gpu-secret')
        self.assertEqual(code, 200)
        self.assertTrue(audio.startswith(b'RIFF'))
        self.assertEqual(self.gpu.calls, [('hi', 'a', 'aurora')])

    def test_bad_input_is_400(self):
        url = self.gpu.url + '/synthesize'
        for body in ({'text': ''}, {'text': 'x', 'speed': 0.01}, {'text': 'x', 'voice': 'wren'}):
            with self.subTest(body=body):
                self.assertEqual(post(url, body, token='gpu-secret')[0], 400)
        self.assertEqual(post(url, None, token='gpu-secret', raw=b'not json')[0], 400)
        big = b'{"text":"' + b'a' * server.MAX_BODY + b'"}'
        self.assertEqual(post(url, None, token='gpu-secret', raw=big)[0], 400)

    def test_service_refuses_to_run_without_a_token(self):
        with self.assertRaises(service.Unauthorized):
            service.check_token('Bearer anything', '')


class RoutingTest(ServerTestCase):
    """Paid accounts go to the GPU; free accounts and guests stay on the CPU."""

    def setUp(self):
        super().setUp()
        self.gpu = FakeGPU()
        self.old = (server._jobs, server.GPU_URL, server.GPU_TOKEN)
        server.GPU_URL, server.GPU_TOKEN = self.gpu.url, 'gpu-secret'
        self.cpu_gen = FakeGenerator()
        cpu = server.JobQueue(self.cpu_gen)
        gpu = server.JobQueue(server.generate_remote, workers=2, name='gpu')
        server._jobs = server.JobRouter(cpu, gpu)
        server._jobs.start()
        self.who = {}

    def tearDown(self):
        server._jobs, server.GPU_URL, server.GPU_TOKEN = self.old
        self.gpu.close()

    def submit(self, text, tier):
        who = None if tier == 'guest' else server.Identity('ip', user=f'u-{tier}', tier=tier)
        job = server._jobs.submit(*server.parse_request({'text': text}), who=who)
        job.done.wait(10)
        return job

    def test_paid_jobs_run_on_the_gpu(self):
        job = self.submit('for a paying listener', 'paid')
        self.assertEqual((job.status, job.queue.name), ('done', 'gpu'))
        self.assertEqual(self.gpu.calls[0][0], 'for a paying listener')
        self.assertEqual(server._jobs.get(job.id), job)
        self.assertTrue(server._cache.get(job.key).startswith(b'RIFF'))

    def test_free_and_guest_jobs_stay_on_the_cpu(self):
        for tier in ('free', 'guest'):
            with self.subTest(tier):
                job = self.submit(f'{tier} text', tier)
                self.assertEqual((job.status, job.queue.name), ('done', 'cpu'))
        self.assertEqual(self.gpu.calls, [])

    def test_a_gpu_failure_is_a_clean_error(self):
        server.GPU_TOKEN = 'wrong'
        job = self.submit('rejected by the gpu', 'paid')
        self.assertEqual(job.status, 'error')
        self.assertNotIn('401', job.error)  # internal details stay in the log
        self.assertEqual(self.gpu.calls, [])


if __name__ == '__main__':
    unittest.main()
