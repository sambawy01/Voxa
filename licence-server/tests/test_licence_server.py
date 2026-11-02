import base64
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import licence_server as ls  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

DEV_A = 'a' * 32
DEV_B = 'b' * 32
DEV_C = 'c' * 32


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = ls.Store(os.path.join(self.dir.name, 'l.db'))
        self.key = self.store.create('test')

    def tearDown(self):
        self.dir.cleanup()

    def test_key_format_and_hashing(self):
        self.assertRegex(self.key, r'^VOXA(-[A-Z2-9]{4}){4}$')
        # Keys are matched ignoring case and dashes; only the hash is stored.
        self.assertEqual(ls.key_hash(self.key), ls.key_hash(self.key.lower().replace('-', '')))
        with ls.sqlite3.connect(self.store.path) as db:
            dump = '\n'.join(db.iterdump())
        self.assertNotIn(ls.normalise_key(self.key), dump)

    def test_two_seats_and_free_reactivation(self):
        a = self.store.activate(self.key, DEV_A, 'Mac')
        self.store.activate(self.key, DEV_B, 'PC')
        # Same device again: same activation, no extra seat.
        self.assertEqual(self.store.activate(self.key, DEV_A, 'Mac')['activation'], a['activation'])
        with self.assertRaises(ls.LicenceError) as e:
            self.store.activate(self.key, DEV_C, 'Third')
        self.assertEqual(e.exception.status, 409)
        self.assertEqual(self.store.active_count(self.key), 2)

    def test_deactivate_frees_a_seat(self):
        a = self.store.activate(self.key, DEV_A, 'Mac')
        self.store.activate(self.key, DEV_B, 'PC')
        self.store.deactivate(self.key, a['activation'])
        self.store.activate(self.key, DEV_C, 'New laptop')
        with self.assertRaises(ls.LicenceError):
            self.store.deactivate(self.key, a['activation'])  # already released

    def test_reset_is_rate_limited_but_admin_is_not(self):
        for left in (2, 1, 0):
            self.store.activate(self.key, DEV_A, 'x')
            self.assertEqual(self.store.reset(self.key), left)
            self.assertEqual(self.store.active_count(self.key), 0)
        with self.assertRaises(ls.LicenceError) as e:
            self.store.reset(self.key)
        self.assertEqual(e.exception.status, 429)
        self.store.activate(self.key, DEV_A, 'x')
        self.store.reset(self.key, admin=True)
        self.assertEqual(self.store.active_count(self.key), 0)

    def test_unknown_key_and_bad_device(self):
        with self.assertRaises(ls.LicenceError) as e:
            self.store.activate('VOXA-NOPE-NOPE-NOPE-NOPE', DEV_A, 'x')
        self.assertEqual(e.exception.status, 404)
        with self.assertRaises(ls.LicenceError) as e:
            self.store.activate(self.key, 'not-hex!', 'x')
        self.assertEqual(e.exception.status, 400)

    def test_payload_never_contains_the_key(self):
        p = self.store.activate(self.key, DEV_A, 'Mac')
        self.assertNotIn(ls.normalise_key(self.key), json.dumps(p))
        self.assertEqual(p['seats'], 2)
        self.assertEqual(p['device'], DEV_A)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = ls.Store(os.path.join(self.dir.name, 'l.db'))
        self.secret = Ed25519PrivateKey.generate()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), ls.make_handler(self.store, self.secret))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        self.key = self.store.create()

    def tearDown(self):
        self.httpd.shutdown()
        self.dir.cleanup()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, json.dumps(body).encode(),
                                     {'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_activate_returns_a_verifiable_licence(self):
        status, body = self.post('/v1/activate', {'key': self.key, 'device_id': DEV_A,
                                                  'device_name': 'Mac'})
        self.assertEqual(status, 200)
        lic = body['licence']
        self.secret.public_key().verify(base64.b64decode(lic['sig']), lic['payload'].encode())
        self.assertEqual(json.loads(lic['payload'])['device'], DEV_A)

    def test_errors_are_json(self):
        self.assertEqual(self.post('/v1/activate', {'key': 'x', 'device_id': DEV_A})[0], 404)
        self.assertEqual(self.post('/v1/nope', {})[0], 404)
        status, body = self.post('/v1/deactivate', {'key': self.key, 'activation': 'x'})
        self.assertEqual(status, 404)
        self.assertIn('error', body)

    def test_full_cycle_over_http(self):
        acts = [self.post('/v1/activate', {'key': self.key, 'device_id': d})[1]
                for d in (DEV_A, DEV_B)]
        self.assertEqual(self.post('/v1/activate', {'key': self.key, 'device_id': DEV_C})[0], 409)
        first = json.loads(acts[0]['licence']['payload'])['activation']
        self.assertEqual(self.post('/v1/deactivate', {'key': self.key, 'activation': first})[0], 200)
        self.assertEqual(self.post('/v1/activate', {'key': self.key, 'device_id': DEV_C})[0], 200)
        status, body = self.post('/v1/reset', {'key': self.key})
        self.assertEqual((status, body['resets_left']), (200, 2))


class SharedVectorTest(unittest.TestCase):
    """The committed test vector, which the Rust verifier checks too."""

    def test_vector_verifies(self):
        path = os.path.join(os.path.dirname(__file__), 'licence_vector.json')
        v = json.load(open(path))
        pub = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(v['test_secret'])).public_key()
        self.assertEqual(pub.public_bytes_raw().hex(), v['public_key'])
        pub.verify(base64.b64decode(v['licence']['sig']), v['licence']['payload'].encode())


if __name__ == '__main__':
    unittest.main()
