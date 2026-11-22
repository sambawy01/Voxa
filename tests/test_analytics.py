"""Voxa's own visitor counter (analytics.py) and its routes in server.py.
    python -m unittest discover -s tests
"""
import hashlib
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analytics  # noqa: E402

PHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1'
MAC = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 Chrome/131.0 Safari/537.36'


class CounterTest(unittest.TestCase):
    def setUp(self):
        self.a = analytics.Analytics(tempfile.mkdtemp())

    def today(self):
        return self.a.summary(1)['days'][0]

    def test_visitors_views_sources_devices(self):
        self.a.page('/', '1.1.1.1', PHONE, 'https://t.co/abc', '', 'fra')
        self.a.page('/podcast', '1.1.1.1', PHONE, 'https://voxavocals.com/', '', 'fra')   # same person, inside the site
        self.a.page('/', '2.2.2.2', MAC, 'https://www.google.com/', '', 'iad')
        self.a.page('/', '3.3.3.3', MAC, '', 'utm_source=newsletter', 'iad')
        self.a.page('/', '4.4.4.4', MAC, '', '', None)
        self.a.page('/book', '4.4.4.4', MAC, '', '', None)                    # a second page, no referrer: not a new arrival
        d = self.today()
        self.assertEqual(d['visitors'], 4)
        self.assertEqual(d['views'], {'/': 4, '/podcast': 1, '/book': 1})
        self.assertEqual(d['page_visitors'], {'/': 4, '/podcast': 1, '/book': 1})
        self.assertEqual(d['sources'], {'X': 1, 'Google': 1, 'Newsletter': 1, 'Direct': 1})
        self.assertEqual(d['devices'], {'Phone or tablet': 1, 'Computer': 3})
        self.assertEqual(d['regions'], {'fra': 1, 'iad': 2})

    def test_bots_and_tests_are_not_visitors(self):
        for ua in ('Twitterbot/1.0', 'curl/8.5', 'Mozilla/5.0 HeadlessChrome/131.0', 'python-requests/2.32', ''):
            self.a.page('/', '9.9.9.9', ua, '', '', 'fra')
        d = self.today()
        self.assertEqual((d['visitors'], d['views'], d['bots']), (0, {}, 5))

    def test_events(self):
        self.assertTrue(self.a.event('podcast', '1.1.1.1', PHONE))
        self.assertTrue(self.a.event('podcast', '1.1.1.1', PHONE))
        self.assertTrue(self.a.event('speak', '2.2.2.2', MAC))
        self.assertFalse(self.a.event('nonsense', '1.1.1.1', PHONE))
        self.assertFalse(self.a.event('speak', '1.1.1.1', 'curl/8'))
        d = self.today()
        self.assertEqual(d['events'], {'podcast': 2, 'speak': 1})
        self.assertEqual(d['event_visitors'], {'podcast': 1, 'speak': 1})

    def test_no_addresses_and_old_days_keep_only_counts(self):
        self.a.page('/', '203.0.113.7', MAC, '', '', 'fra')
        with open(os.path.join(self.a.dir, self.a.day + '.json')) as f:
            raw = f.read()
        self.assertNotIn('203.0.113.7', raw)                     # the address is never written
        self.assertNotIn('salt', json.dumps(self.a.summary(1)))   # nor shown
        # an older day, as it was left at midnight: its salt and hashes go, its counts stay
        old = os.path.join(self.a.dir, '2020-01-01.json')
        with open(old, 'w') as f:
            json.dump({'day': '2020-01-01', 'salt': 'x', 'hashes': ['a'], 'page_hashes': {'/': ['a']}, 'visitors': 1, 'views': {'/': 1}}, f)
        self.a.day = None
        self.a.page('/', '1.1.1.1', MAC, '', '', 'fra')
        with open(old) as f:
            d = json.load(f)
        self.assertNotIn('salt', d)
        self.assertNotIn('hashes', d)
        self.assertEqual(d['visitors'], 1)

    def test_restart_same_day_keeps_visitors(self):
        self.a.page('/', '1.1.1.1', MAC, '', '', 'fra')
        b = analytics.Analytics(os.path.dirname(self.a.dir))
        b.page('/', '1.1.1.1', MAC, '', '', 'fra')      # the same person after a restart
        self.assertEqual(b.summary(1)['days'][0]['visitors'], 1)


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import agentapi
        import server
        from test_agentapi import KEY, make_api
        cls.server, cls.KEY = server, KEY
        server._agent_api = make_api()
        server._analytics = analytics.Analytics(tempfile.mkdtemp())
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.base = f'http://127.0.0.1:{cls.httpd.server_address[1]}'
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.server._agent_api = None
        cls.server._analytics = None

    def req(self, path, body=None, headers=None):
        r = urllib.request.Request(self.base + path, data=body, headers={'User-Agent': MAC, **(headers or {})},
                                   method='POST' if body is not None else 'GET')
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_pages_events_and_private_stats(self):
        self.assertEqual(self.req('/', headers={'Referer': 'https://x.com/voxavocals'})[0], 200)
        self.assertEqual(self.req('/voxa.css')[0], 200)                     # not a page: not counted
        self.assertEqual(self.req('/stats')[0], 200)                        # the stats page itself: not counted
        self.assertEqual(self.req('/api/hit', b'{"e":"speak"}')[0], 204)
        self.assertEqual(self.req('/api/hit', b'x' * 5000)[0], 400)
        self.assertEqual(self.req('/api/stats')[0], 401)
        code, body = self.req('/api/stats?days=7', headers={'Authorization': 'Bearer ' + self.KEY})
        self.assertEqual(code, 200)
        d = json.loads(body)['days'][0]
        self.assertEqual(d['views'], {'/': 1})
        self.assertEqual(d['sources'], {'X': 1})
        self.assertEqual(d['events'], {'speak': 1})


if __name__ == '__main__':
    unittest.main()
