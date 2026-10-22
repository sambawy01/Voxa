"""Payments: Stripe Checkout, the customer portal, and the signed webhook that marks an
account paid in Supabase. Stripe and Supabase are fakes that speak their real formats.
"""
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import billing  # noqa: E402
import server  # noqa: E402
from test_server import Keys, ServerTestCase, URL  # noqa: E402

WHSEC = 'whsec_test123'


def signed(event, secret=WHSEC, at=None):
    payload = json.dumps(event).encode()
    t = str(int(at if at is not None else time.time()))
    sig = hmac.new(secret.encode(), f'{t}.'.encode() + payload, hashlib.sha256).hexdigest()
    return payload, f't={t},v1={sig}'


class FakeServices:
    """Stripe's /v1 API and Supabase's admin users API on one local port."""

    def __init__(self):
        self.stripe_calls, self.users, self.fail_supabase = [], {}, False
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
                assert self.headers['Authorization'] == 'Bearer sk_test_x'
                fields = dict(urllib.parse.parse_qsl(self.body().decode()))
                outer.stripe_calls.append((self.path, fields))
                kind = 'checkout' if 'checkout' in self.path else 'portal'
                self.reply(200, {'url': f'https://stripe.test/{kind}/session'})

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
        payload, header = signed({'id': 'evt_1'})
        self.assertEqual(billing.verify(payload, header, WHSEC)['id'], 'evt_1')

    def test_accepts_when_any_v1_matches(self):
        payload, header = signed({'id': 'evt_2'})
        self.assertEqual(billing.verify(payload, header.replace('v1=', 'v1=deadbeef,v1='), WHSEC)['id'], 'evt_2')

    def test_rejects_forgeries_and_replays(self):
        payload, header = signed({'id': 'evt_3'})
        old_payload, old_header = signed({'id': 'evt_3'}, at=time.time() - 3600)
        cases = {
            'wrong secret': (payload, signed({'id': 'evt_3'}, secret='whsec_other')[1]),
            'tampered body': (payload.replace(b'evt_3', b'evt_4'), header),
            'replayed an hour later': (old_payload, old_header),
            'no header': (payload, None),
            'garbage': (payload, 't=abc,v1=xyz'),
        }
        for name, (body, h) in cases.items():
            with self.subTest(name), self.assertRaises(billing.BadSignature):
                billing.verify(body, h, WHSEC)
        with self.assertRaises(billing.BadSignature):
            billing.verify(payload, header, '')  # no secret configured: nothing verifies


class BillingHTTPTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeServices()
        self.saved = {k: getattr(billing, k) for k in ('STRIPE_API', 'STRIPE_SECRET_KEY', 'STRIPE_PRICE_ID',
                                                        'STRIPE_WEBHOOK_SECRET', 'SUPABASE_SERVICE_ROLE_KEY', 'PRICE_LABEL')}
        billing.STRIPE_API, billing.STRIPE_SECRET_KEY = self.fake.url, 'sk_test_x'
        billing.STRIPE_PRICE_ID, billing.STRIPE_WEBHOOK_SECRET = 'price_123', WHSEC
        billing.SUPABASE_SERVICE_ROLE_KEY, billing.PRICE_LABEL = 'service-key', '$5 a month'
        self.old = (server.SUPABASE_URL, server._auth)
        server.SUPABASE_URL = self.fake.url  # the admin API lives at the project URL
        self.keys = getattr(BillingHTTPTest, '_keys', None) or Keys()
        BillingHTTPTest._keys = self.keys
        server._auth = server.TokenVerifier(URL, fetch_jwks=lambda: self.keys.jwks)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(billing, k, v)
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

    def test_config_offers_the_plan(self):
        _, _, body = self.req('/api/config')
        self.assertEqual(json.loads(body)['billing'], {'price_label': '$5 a month'})

    def test_checkout_needs_an_account_and_sends_the_right_fields(self):
        self.assertEqual(self.call('/api/billing/checkout')[0], 401)
        token = self.keys.token(sub='alice', email='alice@example.com')
        code, body = self.call('/api/billing/checkout', token=token)
        self.assertEqual((code, body['url']), (200, 'https://stripe.test/checkout/session'))
        path, fields = self.fake.stripe_calls[-1]
        self.assertEqual(path, '/v1/checkout/sessions')
        self.assertEqual(fields['mode'], 'subscription')
        self.assertEqual(fields['line_items[0][price]'], 'price_123')
        self.assertEqual(fields['client_reference_id'], 'alice')
        self.assertEqual(fields['subscription_data[metadata][user_id]'], 'alice')
        self.assertEqual(fields['customer_email'], 'alice@example.com')
        self.assertTrue(fields['success_url'].endswith('/?upgraded=1'))

    def test_paid_accounts_manage_instead_of_buying_again(self):
        paid = self.keys.token(sub='bob', app_metadata={'tier': 'paid', 'stripe_customer': 'cus_9'})
        self.assertEqual(self.call('/api/billing/checkout', token=paid)[0], 409)
        code, body = self.call('/api/billing/portal', token=paid)
        self.assertEqual((code, body['url']), (200, 'https://stripe.test/portal/session'))
        self.assertEqual(self.fake.stripe_calls[-1][1]['customer'], 'cus_9')
        never = self.keys.token(sub='carol')
        self.assertEqual(self.call('/api/billing/portal', token=never)[0], 409)

    def webhook(self, event, **kw):
        payload, header = signed(event, **kw)
        return self.call('/api/billing/webhook', raw=payload, headers={'Stripe-Signature': header})

    def test_a_completed_checkout_marks_the_account_paid_and_keeps_its_other_metadata(self):
        code, _ = self.webhook({'id': 'evt_a', 'type': 'checkout.session.completed', 'data': {'object': {
            'mode': 'subscription', 'payment_status': 'paid', 'client_reference_id': 'alice',
            'metadata': {'user_id': 'alice'}, 'customer': 'cus_1', 'subscription': 'sub_1'}}})
        self.assertEqual(code, 200)
        self.assertEqual(self.fake.users['alice'], {'provider': 'email', 'tier': 'paid',
                                                    'stripe_customer': 'cus_1', 'stripe_subscription': 'sub_1'})

    def test_a_cancelled_subscription_returns_the_account_to_free(self):
        self.fake.users['alice'] = {'tier': 'paid', 'stripe_customer': 'cus_1'}
        for kind, status in (('customer.subscription.updated', 'canceled'), ('customer.subscription.deleted', 'active')):
            with self.subTest(kind):
                self.fake.users['alice']['tier'] = 'paid'
                self.webhook({'id': 'evt_b', 'type': kind, 'data': {'object': {
                    'id': 'sub_1', 'status': status, 'customer': 'cus_1', 'metadata': {'user_id': 'alice'}}}})
                self.assertEqual(self.fake.users['alice']['tier'], 'free')
        self.webhook({'id': 'evt_c', 'type': 'customer.subscription.updated', 'data': {'object': {
            'id': 'sub_1', 'status': 'past_due', 'customer': 'cus_1', 'metadata': {'user_id': 'alice'}}}})
        self.assertEqual(self.fake.users['alice']['tier'], 'paid')  # access kept while Stripe retries

    def test_forged_or_unrelated_events_change_nothing(self):
        event = {'id': 'evt_x', 'type': 'checkout.session.completed', 'data': {'object': {
            'mode': 'subscription', 'payment_status': 'paid', 'client_reference_id': 'mallory'}}}
        self.assertEqual(self.webhook(event, secret='whsec_guess')[0], 400)
        unpaid = json.loads(json.dumps(event))
        unpaid['data']['object']['payment_status'] = 'unpaid'
        self.assertEqual(self.webhook(unpaid)[0], 200)
        self.assertEqual(self.webhook({'id': 'evt_y', 'type': 'invoice.created', 'data': {'object': {}}})[0], 200)
        self.assertNotIn('mallory', self.fake.users)

    def test_a_supabase_outage_asks_stripe_to_retry(self):
        self.fake.fail_supabase = True
        code, _ = self.webhook({'id': 'evt_z', 'type': 'checkout.session.completed', 'data': {'object': {
            'mode': 'subscription', 'payment_status': 'paid', 'client_reference_id': 'dave'}}})
        self.assertEqual(code, 500)

    def test_without_configuration_the_routes_do_not_exist(self):
        billing.STRIPE_SECRET_KEY = ''
        self.assertEqual(self.call('/api/billing/checkout')[0], 404)
        _, _, body = self.req('/api/config')
        self.assertIsNone(json.loads(body)['billing'])


import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

if __name__ == '__main__':
    unittest.main()
