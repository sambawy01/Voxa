# Turning on the paid plan (Paddle)

Paddle is Voxa's reseller and merchant of record. It sells the subscription, charges VAT and
sales tax worldwide, handles refunds and chargebacks, and pays out once a month, for example
to Payoneer. Everything is built and tested. Payments switch on when the server has the
Paddle keys below and Supabase's service-role key. Accounts must be on first
(docs/SUPABASE.md). Every step works in a browser, including on an iPad.

Start in the **sandbox** (sandbox-vendors.paddle.com) and buy the plan with Paddle's test
card `4242 4242 4242 4242`, any future date, CVC `100`. Then repeat the steps in the live
account.

## 0. Before the review

Paddle checks the website before it approves a live account. Voxa already has public pages
for [pricing](/pricing), [terms](/terms), [privacy](/privacy) and [refunds](/refunds). The
terms include Paddle's required reseller wording. Set these so the pages show real details:

```bash
fly secrets set -a voxa-zpukmw VOXA_OPERATOR="<your full legal name>" \
  VOXA_CONTACT_EMAIL=<support email> VOXA_PRICE_LABEL="\$5 a month" VOXA_PUBLIC_URL=https://voxavocals.com
```

Apply with the domain (voxavocals.com), not the fly.dev address. Describe Voxa as
"text-to-speech web app (SaaS) with a monthly subscription".

## 1. Product and price

**Catalog → Products → New product**: name it (e.g. "Voxa Unlimited"), tax category
"Standard digital goods". Add a **recurring** monthly price and copy its id (`pri_...`).

## 2. Keys

**Developer tools → Authentication**:

- Create an **API key** with these permissions: customer portal sessions (write), customers
  (read) and subscriptions (read). It starts `pdl_live_apikey_` (or `pdl_sdbx_apikey_` in the
  sandbox).
- Create a **client-side token**. It starts `live_` (or `test_`). It is public: the page uses
  it to open the checkout.

## 3. Webhook

**Developer tools → Notifications → New destination**:

- URL: `https://voxavocals.com/api/billing/webhook`
- Events: `subscription.created`, `subscription.activated`, `subscription.updated`,
  `subscription.canceled`, `subscription.past_due`, `subscription.paused`,
  `subscription.resumed`

Copy the destination's **secret key**.

## 4. Checkout settings

**Checkout → Checkout settings**: set the **default payment link** to
`https://voxavocals.com/checkout`, and add `voxavocals.com` under approved domains if asked.

## 5. Payouts

**Business account → Payouts**: choose **Payoneer** and connect your Payoneer account.
Paddle pays in US dollars once a month, when the balance is at least $100.

## 6. The secrets

```bash
fly secrets set -a voxa-zpukmw PADDLE_API_KEY=... PADDLE_CLIENT_TOKEN=... \
  PADDLE_PRICE_ID=pri_... PADDLE_WEBHOOK_SECRET=... SUPABASE_SERVICE_ROLE_KEY=...
```

Never put these in the repository or in a chat. `SUPABASE_SERVICE_ROLE_KEY` is under Supabase
**Project settings → API**. It bypasses all access rules, so it lives only in Fly secrets.

## How it works

- **Upgrade** (signed-in free accounts): the server returns the checkout details, including
  the account id plus a signature only the server can make. The page opens Paddle's checkout
  on `/checkout`. That page is not cross-origin isolated, because Paddle's checkout frame
  could not load inside Voxa's isolated pages.
- **Webhook**: Paddle signs each event (`Paddle-Signature: ts=...;h1=...`, HMAC-SHA256 of
  `ts:body`). Voxa checks the signature, then the account signature in the subscription's
  custom data, then sets `app_metadata.tier` in Supabase:
  - `active`, `trialing` or `past_due` means paid. Access is kept while Paddle retries a card.
  - Anything else means free, but only if it is the subscription the account holds.
  - Events older than the last one applied are skipped.
- **Manage plan** (paid accounts): opens a Paddle customer-portal session, where subscribers
  update their card or cancel.
- **Back on Voxa**: the page refreshes the sign-in until the paid plan shows. The webhook
  can take a few seconds.
