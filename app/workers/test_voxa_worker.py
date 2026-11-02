import base64
import io
import json
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import voxa_worker  # noqa: E402
from reference_worker import ToneEngine  # noqa: E402


def run(lines, engine=None):
    out = io.StringIO()
    voxa_worker.serve(engine or ToneEngine(), io.StringIO(''.join(l + '\n' for l in lines)), out)
    return [json.loads(l) for l in out.getvalue().splitlines()]


class Protocol(unittest.TestCase):
    def test_hello_synthesize_shutdown(self):
        r = run([json.dumps({'id': 1, 'op': 'hello', 'protocol': 1}),
                 json.dumps({'id': 2, 'op': 'synthesize', 'text': 'two words', 'voice': 'low'}),
                 json.dumps({'id': 3, 'op': 'shutdown'}),
                 json.dumps({'id': 4, 'op': 'hello', 'protocol': 1})])
        self.assertEqual([x['id'] for x in r], [1, 2, 3], 'stops after shutdown')
        self.assertEqual(r[0]['voices'], ['low', 'high'])
        raw = base64.b64decode(r[1]['samples'])
        samples = struct.unpack(f'<{len(raw) // 4}f', raw)
        self.assertEqual(len(samples), 2 * (1920 + 640))
        self.assertTrue(max(samples) <= 0.31)

    def test_errors_are_replies_not_crashes(self):
        r = run(['not json',
                 json.dumps({'id': 1, 'op': 'hello', 'protocol': 99}),
                 json.dumps({'id': 2, 'op': 'synthesize', 'text': 'x', 'voice': 'nobody'}),
                 json.dumps({'id': 3, 'op': 'synthesize', 'text': '  ', 'voice': 'low'}),
                 json.dumps({'id': 4, 'op': 'dance'}),
                 json.dumps({'id': 5, 'op': 'synthesize', 'text': 'x', 'voice': 'low'})],
                ToneEngine(fail_after=1))
        self.assertTrue(all(not x['ok'] for x in r))
        self.assertIn('bad JSON', r[0]['error'])
        self.assertIn('protocol 1', r[1]['error'])
        self.assertIn('unknown voice', r[2]['error'])
        self.assertIn('no text', r[3]['error'])
        self.assertIn('unknown op', r[4]['error'])
        self.assertIn('out of GPU memory', r[5]['error'])


if __name__ == '__main__':
    unittest.main()
