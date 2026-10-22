"""Voxa payments: Stripe Checkout for the paid plan, Stripe webhooks to mark accounts paid.

Configuration (all on the server; see docs/STRIPE.md):
    STRIPE_SECRET_KEY        sk_live_... or sk_test_...
    STRIPE_PRICE_ID          price_... of the paid plan (a recurring price)
    STRIPE_WEBHOOK_SECRET    whsec_... of the webhook endpoint /api/billing/webhook
    SUPABASE_SERVICE_ROLE_KEY  lets the webhook set an account's tier

The tier lives in the account's app_metadata ("tier": "paid"), which is in every sign-in
token and which users cannot edit. Only the webhook writes it, and only after checking
Stripe's signature. The service-role key is used for nothing else.
"""
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger('voxa.billing')

STRIPE_API = os.environ.get('STRIPE_API_BASE', 'https://api.stripe.com').rstrip('/')
STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PRICE_ID = os.environ.get('STRIPE_PRICE_ID', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
SUPABASE_SERVICE_ROLE_KEY = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
# What the page shows on the upgrade button, e.g. "$5 a month". Optional.
PRICE_LABEL = os.environ.get('VOXA_PRICE_LABEL', '')
SIGNATURE_TOLERANCE = 300  # seconds; Stripe's own default
# Subscription states that keep an account paid. past_due keeps access while Stripe retries.
PAID_STATUSES = {'active', 'trialing', 'past_due'}


class BillingError(Exception):
    """Something the user should be told, e.g. billing is not set up."""


class BadSignature(Exception):
    pass


def enabled(supabase_url):
    return bool(STRIPE_SECRET_KEY and STRIPE_PRICE_ID and STRIPE_WEBHOOK_SECRET
                and SUPABASE_SERVICE_ROLE_KEY and supabase_url)


def _stripe(path, fields):
    """POST form fields to the Stripe API; returns the JSON object."""
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(f'{STRIPE_API}{path}', data=data, headers={
        'Authorization': f'Bearer {STRIPE_SECRET_KEY}',
        'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read()[:500]
        log.error('Stripe %s failed: %s %s', path, e.code, detail)
        raise BillingError('The payment service is not available right now. Please try again.')


def checkout_url(who, return_to):
    """A Stripe Checkout page for this signed-in account to start the paid plan."""
    fields = {
        'mode': 'subscription',
        'line_items[0][price]': STRIPE_PRICE_ID,
        'line_items[0][quantity]': '1',
        'success_url': f'{return_to}?upgraded=1',
        'cancel_url': return_to,
        'client_reference_id': who.user,
        'metadata[user_id]': who.user,
        'subscription_data[metadata][user_id]': who.user,
        'allow_promotion_codes': 'true',
    }
    if who.customer:
        fields['customer'] = who.customer  # a returning subscriber keeps one Stripe customer
    elif who.email:
        fields['customer_email'] = who.email
    return _stripe('/v1/checkout/sessions', fields)['url']


def portal_url(who, return_to):
    """Stripe's customer portal, where a subscriber changes card, plan or cancels."""
    if not who.customer:
        raise BillingError('There is no subscription on this account yet.')
    return _stripe('/v1/billing_portal/sessions', {'customer': who.customer, 'return_url': return_to})['url']


def verify(payload, header, secret=None, now=None):
    """Check a Stripe-Signature header against the raw body. Returns the parsed event."""
    secret = secret if secret is not None else STRIPE_WEBHOOK_SECRET
    now = time.time() if now is None else now
    items = [part.split('=', 1) for part in (header or '').split(',') if '=' in part]
    timestamps = [v for k, v in items if k == 't']
    signatures = [v for k, v in items if k == 'v1']
    if not secret or len(timestamps) != 1 or not signatures or not timestamps[0].isdigit():
        raise BadSignature('missing or malformed signature')
    if abs(now - int(timestamps[0])) > SIGNATURE_TOLERANCE:
        raise BadSignature('signature too old')
    expected = hmac.new(secret.encode(), timestamps[0].encode() + b'.' + payload, hashlib.sha256).hexdigest()
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


def set_tier(supabase_url, user_id, tier, customer=None, subscription=None):
    """Write the tier (and Stripe ids) into the account's app_metadata, keeping other keys."""
    user = _supabase(supabase_url, 'GET', f'/admin/users/{urllib.parse.quote(user_id)}')
    meta = dict(user.get('app_metadata') or {})
    meta['tier'] = tier
    if customer:
        meta['stripe_customer'] = customer
    if subscription:
        meta['stripe_subscription'] = subscription
    _supabase(supabase_url, 'PUT', f'/admin/users/{urllib.parse.quote(user_id)}', {'app_metadata': meta})
    log.info('account %s is now %s', user_id, tier)


def handle_event(event, supabase_url):
    """Apply a verified Stripe event. Returns what it did, for the log and the tests."""
    kind, obj = event.get('type'), (event.get('data') or {}).get('object') or {}
    user = (obj.get('metadata') or {}).get('user_id') or obj.get('client_reference_id')
    if kind == 'checkout.session.completed':
        if not user or obj.get('mode') != 'subscription':
            return 'ignored'
        if obj.get('payment_status') not in ('paid', 'no_payment_required'):
            return 'ignored: not paid yet'
        set_tier(supabase_url, user, 'paid', obj.get('customer'), obj.get('subscription'))
        return 'paid'
    if kind in ('customer.subscription.updated', 'customer.subscription.deleted'):
        if not user:
            return 'ignored'
        paid = kind == 'customer.subscription.updated' and obj.get('status') in PAID_STATUSES
        set_tier(supabase_url, user, 'paid' if paid else 'free', obj.get('customer'), obj.get('id'))
        return 'paid' if paid else 'free'
    return 'ignored'
