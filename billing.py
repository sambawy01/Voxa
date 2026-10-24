"""Voxa payments: Paddle Billing for the paid plan, Paddle webhooks to mark accounts paid.

Paddle is the merchant of record: it sells the subscription, charges VAT and sales tax, and
pays out (for example to Payoneer). Stripe does not onboard sellers in Egypt.

Configuration (all on the server; see docs/PADDLE.md):
    PADDLE_API_KEY           pdl_live_apikey_... or pdl_sdbx_apikey_... (sandbox)
    PADDLE_CLIENT_TOKEN      live_... or test_...: public, lets the page open Paddle's checkout
    PADDLE_PRICE_ID          pri_... of the paid plan (a recurring price)
    PADDLE_WEBHOOK_SECRET    the endpoint secret of the notification destination
    SUPABASE_SERVICE_ROLE_KEY  lets the webhook set an account's tier

The tier lives in the account's app_metadata ("tier": "paid"), which is in every sign-in
token and which users cannot edit. Only the webhook writes it, and only after checking
Paddle's signature. Checkout runs in the browser, so the account id it carries is signed
by this server and checked again when Paddle reports the subscription.
"""
import datetime
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import plans

log = logging.getLogger('voxa.billing')

PADDLE_API_KEY = os.environ.get('PADDLE_API_KEY', '')
PADDLE_CLIENT_TOKEN = os.environ.get('PADDLE_CLIENT_TOKEN', '')
PADDLE_PRICE_ID = os.environ.get('PADDLE_PRICE_ID', '')
PADDLE_WEBHOOK_SECRET = os.environ.get('PADDLE_WEBHOOK_SECRET', '')
SUPABASE_SERVICE_ROLE_KEY = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
# What the page shows on the upgrade button, e.g. "$5 a month". Optional.
PRICE_LABEL = os.environ.get('VOXA_PRICE_LABEL', '')
# Paddle signs each delivery afresh, and applying an event twice changes nothing, so a few
# minutes covers clock drift without opening a useful replay window.
SIGNATURE_TOLERANCE = 300
# Subscription states that keep an account paid. past_due keeps access while Paddle retries.
PAID_STATUSES = {'active', 'trialing', 'past_due'}
SUBSCRIPTION_EVENTS = {'subscription.created', 'subscription.activated', 'subscription.updated',
                       'subscription.canceled', 'subscription.past_due', 'subscription.paused',
                       'subscription.resumed', 'subscription.trialing'}


def sandbox():
    return '_sdbx_' in PADDLE_API_KEY


def api_base():
    return os.environ.get('PADDLE_API_BASE') or (
        'https://sandbox-api.paddle.com' if sandbox() else 'https://api.paddle.com')


class BillingError(Exception):
    """Something the user should be told, e.g. billing is not set up."""


class BadSignature(Exception):
    pass


def enabled(supabase_url):
    return bool(PADDLE_API_KEY and PADDLE_CLIENT_TOKEN and PADDLE_PRICE_ID and PADDLE_WEBHOOK_SECRET
                and SUPABASE_SERVICE_ROLE_KEY and supabase_url)


def _paddle(method, path, body=None):
    """Call the Paddle API; returns the JSON object."""
    req = urllib.request.Request(f'{api_base()}{path}', method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Authorization': f'Bearer {PADDLE_API_KEY}',
                                          'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        log.error('Paddle %s %s failed: %s %s', method, path, e.code, e.read()[:500])
        raise BillingError('The payment service is not available right now. Please try again.')
    except (urllib.error.URLError, TimeoutError) as e:
        log.error('Paddle %s %s failed: %s', method, path, e)
        raise BillingError('The payment service is not available right now. Please try again.')


def sign_user(user_id, secret=None):
    """A tag proving this server put the account id into the checkout."""
    secret = secret if secret is not None else PADDLE_WEBHOOK_SECRET
    return hmac.new(secret.encode(), b'voxa-user:' + user_id.encode(), hashlib.sha256).hexdigest()[:32]


def checked_user(custom_data, secret=None):
    """The account id from a subscription's custom data, if this server signed it."""
    data = custom_data if isinstance(custom_data, dict) else {}
    user, tag = data.get('user_id'), data.get('user_sig')
    if isinstance(user, str) and isinstance(tag, str) and hmac.compare_digest(sign_user(user, secret), tag):
        return user
    return None


def checkout(who, return_to, pack=None):
    """What the checkout page needs to open Paddle's checkout for this signed-in account: the
    Studio subscription, or a one-off HD pack (`pack` names one of plans.PACKS)."""
    customer = {'id': who.customer} if who.customer else ({'email': who.email} if who.email else {})
    price = PADDLE_PRICE_ID
    if pack:
        price = os.environ.get(plans.PACKS[pack]['env'], '')
    return {'environment': 'sandbox' if sandbox() else 'production', 'token': PADDLE_CLIENT_TOKEN,
            'price_id': price, 'customer': customer,
            'custom_data': {'user_id': who.user, 'user_sig': sign_user(who.user)},
            'success_url': f'{return_to}?{"pack=" + pack if pack else "upgraded=1"}'}


def portal_url(who):
    """Paddle's customer portal, where a subscriber changes card or cancels."""
    if not who.customer:
        raise BillingError('There is no subscription on this account yet.')
    body = {'subscription_ids': [who.subscription]} if who.subscription else {}
    reply = _paddle('POST', f'/customers/{urllib.parse.quote(who.customer)}/portal-sessions', body)
    try:
        return reply['data']['urls']['general']['overview']
    except (KeyError, TypeError):
        raise BillingError('The payment service is not available right now. Please try again.')


def verify(payload, header, secret=None, now=None):
    """Check a Paddle-Signature header (ts=...;h1=...) against the raw body. Returns the event."""
    secret = secret if secret is not None else PADDLE_WEBHOOK_SECRET
    now = time.time() if now is None else now
    items = [part.split('=', 1) for part in (header or '').split(';') if '=' in part]
    timestamps = [v for k, v in items if k == 'ts']
    signatures = [v for k, v in items if k == 'h1']
    if not secret or len(timestamps) != 1 or not signatures or not timestamps[0].isdigit():
        raise BadSignature('missing or malformed signature')
    if abs(now - int(timestamps[0])) > SIGNATURE_TOLERANCE:
        raise BadSignature('signature too old')
    expected = hmac.new(secret.encode(), timestamps[0].encode() + b':' + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in signatures):
        raise BadSignature('signature does not match')
    return json.loads(payload)


def _supabase(supabase_url, method, path, body=None):
    req = urllib.request.Request(f'{supabase_url}/auth/v1{path}', method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={'apikey': SUPABASE_SERVICE_ROLE_KEY,
                                          'Authorization': f'Bearer {SUPABASE_SERVICE_ROLE_KEY}',
                                          'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _when(stamp):
    """An RFC 3339 time from Paddle as a datetime, or None."""
    try:
        return datetime.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return None


def apply_subscription(supabase_url, user_id, sub, occurred_at):
    """Set the account's tier from a Paddle subscription, keeping other app_metadata keys.

    Events can arrive out of order, so an event older than the last one applied is skipped.
    An account returns to free only when the subscription that ended is the one it holds,
    so cancelling an old subscription never removes a newer one.
    """
    user = _supabase(supabase_url, 'GET', f'/admin/users/{urllib.parse.quote(user_id)}')
    meta = dict(user.get('app_metadata') or {})
    if _when(occurred_at) and _when(meta.get('paddle_event_at')) and _when(occurred_at) < _when(meta['paddle_event_at']):
        return 'ignored: older than the last change'
    paid = sub.get('status') in PAID_STATUSES
    if not paid and meta.get('paddle_subscription') not in (None, sub.get('id')):
        return 'ignored: not the current subscription'
    meta['tier'] = 'paid' if paid else 'free'
    meta['paddle_customer'] = sub.get('customer_id') or meta.get('paddle_customer')
    meta['paddle_subscription'] = sub.get('id')
    if occurred_at:
        meta['paddle_event_at'] = occurred_at
    _supabase(supabase_url, 'PUT', f'/admin/users/{urllib.parse.quote(user_id)}', {'app_metadata': meta})
    log.info('account %s is now %s (%s)', user_id, meta['tier'], sub.get('status'))
    return meta['tier']


def handle_event(event, supabase_url, credit=None):
    """Apply a verified Paddle event. Returns what it did, for the log and the tests.

    A completed transaction that is not a subscription payment is an HD pack:
    `credit(user, price_id, quantity, transaction_id)` adds it to the account's wallet
    (once per transaction, however often Paddle repeats the event)."""
    if event.get('event_type') == 'transaction.completed':
        tx = event.get('data') or {}
        if tx.get('subscription_id'):
            return 'ignored: subscription payment'
        user = checked_user(tx.get('custom_data'))
        if not user or credit is None:
            log.warning('Paddle transaction %s has no signed account id', tx.get('id'))
            return 'ignored: no signed account id'
        done = [credit(user, (item.get('price') or {}).get('id'), item.get('quantity'), tx.get('id'))
                for item in tx.get('items') or []]
        return '; '.join(done) or 'ignored: no items'
    if event.get('event_type') not in SUBSCRIPTION_EVENTS:
        return 'ignored'
    sub = event.get('data') or {}
    user = checked_user(sub.get('custom_data'))
    if not user:
        log.warning('Paddle %s for %s has no signed account id', event.get('event_type'), sub.get('id'))
        return 'ignored: no signed account id'
    return apply_subscription(supabase_url, user, sub, event.get('occurred_at'))
