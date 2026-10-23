"""Payments: Paddle checkout details, the customer portal, and the signed webhook that marks an
account paid in Supabase. Paddle and Supabase are fakes that speak their real formats.
"""
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import billing  # noqa: E402
import server  # noqa: E402
from test_server import Keys, ServerTestCase, URL  # noqa: E402

SECRET = 'pdl_ntfset_test123'


def signed(event, secret=SECRET, at=None):
    payload = json.dumps(event).encode()
    ts = str(int(at if at is not None else time.time()))
    sig = hmac.new(secret.encode(), f'{ts}:'.encode() + payload, hashlib.sha256).hexdigest()
    return payload, f'ts={ts};h1={sig}'


def sub_event(kind, user='alice', status='active', sub='sub_1', customer='ctm_1', at='2026-10-23T10:00:00.000000Z',
              sig=None):
    custom = {'user_id': user, 'user_sig': sig if sig is not None else billing.sign_user(user, SECRET)} if user else None
    return {'event_id': 'evt_' + kind, 'event_type': kind, 'occurred_at': at,
            'data': {'id': sub, 'status': status, 'customer_id': customer, 'custom_data': custom}}


class FakeServices:
    """Paddle's API and Supabase's admin users API on one local port."""

    def __init__(self):
        self.paddle_calls, self.users, self.fail_supabase = [], {}, False
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def body(self):
                return self.rfile.read(int(self.headers.get('Content-Length') or 0))

            def do_POST(self):
                assert self.headers['Authorization'] == 'Bearer pdl_sdbx_apikey_x'
                outer.paddle_calls.append((self.path, json.loads(self.body() or b'{}')))
                self.reply(201, {'data': {'id': 'cpls_1', 'urls': {
                    'general': {'overview': 'https://customer-portal.paddle.com/cpl_1?token=t'}, 'subscriptions': []}}})

            def do_GET(self):
                if outer.fail_supabase:
                    return self.reply(503, {})
                assert self.headers['apikey'] == 'service-key'
                uid = self.path.rsplit('/', 1)[-1]
                self.reply(200, {'id': uid, 'app_metadata': outer.users.get(uid, {'provider': 'email'})})

            def do_PUT(self):
                uid = self.path.rsplit('/', 1)[-1]
                outer.users[uid] = json.loads(self.body())['app_metadata']
                self.reply(200, {'id': uid})

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.httpd.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


class VerifyTest(unittest.TestCase):
    def test_accepts_a_genuine_signature(self):
        payload, header = signed({'event_id': 'evt_1'})
        self.assertEqual(billing.verify(payload, header, SECRET)['event_id'], 'evt_1')

    def test_accepts_when_any_h1_matches(self):
        # During a secret rotation Paddle sends one h1 per secret.
        payload, header = signed({'event_id': 'evt_2'})
        self.assertEqual(billing.verify(payload, header.replace('h1=', 'h1=deadbeef;h1='), SECRET)['event_id'], 'evt_2')

    def test_rejects_forgeries_and_replays(self):
        payload, header = signed({'event_id': 'evt_3'})
        old_payload, old_header = signed({'event_id': 'evt_3'}, at=time.time() - 3600)
        cases = {
            'wrong secret': (payload, signed({'event_id': 'evt_3'}, secret='other')[1]),
            'tampered body': (payload.replace(b'evt_3', b'evt_4'), header),
            'replayed an hour later': (old_payload, old_header),
            'no header': (payload, None),
            'garbage': (payload, 'ts=abc;h1=xyz'),
            'Stripe format': (payload, header.replace('ts=', 't=').replace(';h1=', ',v1=')),
        }
        for name, (body, h) in cases.items():
            with self.subTest(name), self.assertRaises(billing.BadSignature):
                billing.verify(body, h, SECRET)
        with self.assertRaises(billing.BadSignature):
            billing.verify(payload, header, '')  # no secret configured: nothing verifies

    def test_account_ids_must_carry_this_servers_signature(self):
        tag = billing.sign_user('alice', SECRET)
        self.assertEqual(billing.checked_user({'user_id': 'alice', 'user_sig': tag}, SECRET), 'alice')
        for data in ({'user_id': 'mallory', 'user_sig': tag}, {'user_id': 'alice'}, {'user_id': 'alice', 'user_sig': 5},
                     None, 'alice', {}):
            with self.subTest(data=data):
                self.assertIsNone(billing.checked_user(data, SECRET))


class BillingHTTPTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeServices()
        names = ('PADDLE_API_KEY', 'PADDLE_CLIENT_TOKEN', 'PADDLE_PRICE_ID', 'PADDLE_WEBHOOK_SECRET',
                 'SUPABASE_SERVICE_ROLE_KEY', 'PRICE_LABEL')
        self.saved = {k: getattr(billing, k) for k in names}
        self.saved_base = os.environ.get('PADDLE_API_BASE')
        os.environ['PADDLE_API_BASE'] = self.fake.url
        billing.PADDLE_API_KEY, billing.PADDLE_CLIENT_TOKEN = 'pdl_sdbx_apikey_x', 'test_client_token'
        billing.PADDLE_PRICE_ID, billing.PADDLE_WEBHOOK_SECRET = 'pri_123', SECRET
        billing.SUPABASE_SERVICE_ROLE_KEY, billing.PRICE_LABEL = 'service-key', '$5 a month'
        self.old = (server.SUPABASE_URL, server._auth)
        server.SUPABASE_URL = self.fake.url  # the admin API lives at the project URL
        self.keys = getattr(BillingHTTPTest, '_keys', None) or Keys()
        BillingHTTPTest._keys = self.keys
        server._auth = server.TokenVerifier(URL, fetch_jwks=lambda: self.keys.jwks)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(billing, k, v)
        if self.saved_base is None:
            os.environ.pop('PADDLE_API_BASE', None)
        else:
            os.environ['PADDLE_API_BASE'] = self.saved_base
        server.SUPABASE_URL, server._auth = self.old
        self.fake.httpd.shutdown()

    def call(self, path, body=None, token=None, headers=None, raw=None):
        h = {'Content-Type': 'application/json', **(headers or {})}
        if token:
            h['Authorization'] = f'Bearer {token}'
        data = raw if raw is not None else json.dumps(body or {}).encode()
        req = urllib.request.Request(self.base + path, data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    def webhook(self, event, **kw):
        payload, header = signed(event, **kw)
        return self.call('/api/billing/webhook', raw=payload, headers={'Paddle-Signature': header})

    def test_config_offers_the_plan_and_the_public_token(self):
        _, _, body = self.req('/api/config')
        self.assertEqual(json.loads(body)['billing'], {'price_label': '$5 a month',
                                                       'paddle': {'token': 'test_client_token', 'environment': 'sandbox'}})

    def test_checkout_needs_an_account_and_carries_a_signed_account_id(self):
        self.assertEqual(self.call('/api/billing/checkout')[0], 401)
        token = self.keys.token(sub='alice', email='alice@example.com')
        code, body = self.call('/api/billing/checkout', token=token)
        self.assertEqual(code, 200)
        c = body['checkout']
        self.assertEqual((c['environment'], c['token'], c['price_id']), ('sandbox', 'test_client_token', 'pri_123'))
        self.assertEqual(c['customer'], {'email': 'alice@example.com'})
        self.assertEqual(billing.checked_user(c['custom_data'], SECRET), 'alice')
        self.assertTrue(c['success_url'].endswith('/?upgraded=1'))
        self.assertEqual(self.fake.paddle_calls, [])  # checkout itself runs in the browser
        # A returning subscriber keeps one Paddle customer.
        back = self.keys.token(sub='erin', email='e@example.com', app_metadata={'tier': 'free', 'paddle_customer': 'ctm_7'})
        self.assertEqual(self.call('/api/billing/checkout', token=back)[1]['checkout']['customer'], {'id': 'ctm_7'})

    def test_paid_accounts_manage_instead_of_buying_again(self):
        paid = self.keys.token(sub='bob', app_metadata={'tier': 'paid', 'paddle_customer': 'ctm_9',
                                                        'paddle_subscription': 'sub_9'})
        self.assertEqual(self.call('/api/billing/checkout', token=paid)[0], 409)
        code, body = self.call('/api/billing/portal', token=paid)
        self.assertEqual((code, body['url']), (200, 'https://customer-portal.paddle.com/cpl_1?token=t'))
        self.assertEqual(self.fake.paddle_calls[-1], ('/customers/ctm_9/portal-sessions', {'subscription_ids': ['sub_9']}))
        never = self.keys.token(sub='carol')
        self.assertEqual(self.call('/api/billing/portal', token=never)[0], 409)

    def test_an_active_subscription_marks_the_account_paid_and_keeps_its_other_metadata(self):
        code, body = self.webhook(sub_event('subscription.created'))
        self.assertEqual((code, body), (200, {'received': True}))
        self.assertEqual(self.fake.users['alice'], {
            'provider': 'email', 'tier': 'paid', 'paddle_customer': 'ctm_1', 'paddle_subscription': 'sub_1',
            'paddle_event_at': '2026-10-23T10:00:00.000000Z'})

    def test_cancelled_and_paused_return_to_free_but_past_due_keeps_access(self):
        self.webhook(sub_event('subscription.created'))
        self.webhook(sub_event('subscription.past_due', status='past_due', at='2026-10-24T10:00:00Z'))
        self.assertEqual(self.fake.users['alice']['tier'], 'paid')  # kept while Paddle retries the card
        self.webhook(sub_event('subscription.canceled', status='canceled', at='2026-10-25T10:00:00Z'))
        self.assertEqual(self.fake.users['alice']['tier'], 'free')
        self.webhook(sub_event('subscription.resumed', status='active', sub='sub_2', at='2026-10-26T10:00:00Z'))
        self.webhook(sub_event('subscription.paused', status='paused', sub='sub_2', at='2026-10-27T10:00:00Z'))
        self.assertEqual(self.fake.users['alice']['tier'], 'free')

    def test_late_or_stale_events_do_not_undo_newer_ones(self):
        self.webhook(sub_event('subscription.created', sub='sub_new', at='2026-10-23T10:00:00Z'))
        # An old subscription ending, or an event delivered late, leaves the account paid.
        self.webhook(sub_event('subscription.canceled', status='canceled', sub='sub_old', at='2026-10-23T11:00:00Z'))
        self.webhook(sub_event('subscription.updated', status='canceled', sub='sub_new', at='2026-10-22T09:00:00Z'))
        self.assertEqual(self.fake.users['alice']['tier'], 'paid')
        self.assertEqual(self.fake.users['alice']['paddle_subscription'], 'sub_new')

    def test_forged_unsigned_or_unrelated_events_change_nothing(self):
        self.assertEqual(self.webhook(sub_event('subscription.created', user='mallory'), secret='guess')[0], 400)
        # Someone pays with another account's id but cannot sign it: ignored.
        self.assertEqual(self.webhook(sub_event('subscription.created', user='mallory', sig='0' * 32))[0], 200)
        self.assertEqual(self.webhook(sub_event('subscription.created', user=None))[0], 200)
        self.assertEqual(self.webhook({'event_id': 'e', 'event_type': 'transaction.completed', 'data': {}})[0], 200)
        self.assertEqual(self.fake.users, {})

    def test_a_supabase_outage_asks_paddle_to_retry(self):
        self.fake.fail_supabase = True
        self.assertEqual(self.webhook(sub_event('subscription.created', user='dave'))[0], 500)

    def test_without_configuration_the_routes_do_not_exist(self):
        billing.PADDLE_API_KEY = ''
        self.assertEqual(self.call('/api/billing/checkout')[0], 404)
        _, _, body = self.req('/api/config')
        self.assertIsNone(json.loads(body)['billing'])


class PagesTest(ServerTestCase):
    def test_checkout_page_is_not_isolated_and_the_legal_pages_exist(self):
        code, headers, page = self.req('/checkout')
        self.assertEqual(code, 200)
        self.assertNotIn('Cross-Origin-Embedder-Policy', headers)
        self.assertIn(b'https://cdn.paddle.com/paddle/v2/paddle.js', page)
        for path, needle in (('/terms', b'Paddle.com is the Merchant of Record for all our orders'),
                             ('/refunds', b'within 14 days'), ('/privacy', b'Paddle'), ('/pricing', b'Paid plan')):
            code, headers, page = self.req(path)
            self.assertEqual(code, 200, path)
            self.assertIn(needle, page, path)
            self.assertNotIn(b'{{', page, path)
        _, _, home = self.req('/')
        for link in (b'href="/pricing"', b'href="/terms"', b'href="/privacy"', b'href="/refunds"'):
            self.assertIn(link, home)

    def test_operator_details_are_escaped(self):
        saved = server.OPERATOR, server.CONTACT_EMAIL
        server.OPERATOR, server.CONTACT_EMAIL = 'Ada <b>L</b>', 'help@voxavocals.com'
        try:
            _, _, page = self.req('/terms')
        finally:
            server.OPERATOR, server.CONTACT_EMAIL = saved
        self.assertIn(b'Ada &lt;b&gt;L&lt;/b&gt;', page)
        self.assertIn(b'mailto:help@voxavocals.com', page)


if __name__ == '__main__':
    unittest.main()
