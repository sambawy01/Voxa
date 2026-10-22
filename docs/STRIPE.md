# Turning on the paid plan (Stripe)

Everything is built and tested. Payments switch on when the server has a Stripe account,
a price, a webhook secret, and Supabase's service-role key. Accounts must be on first
(docs/SUPABASE.md). Every step below works in a browser, including on an iPad.

Do it in Stripe's **test mode** first (the "Test mode" switch in the dashboard), buy the plan
with the card `4242 4242 4242 4242`, then repeat with live keys.

## 1. The product and price

**Product catalogue → Add product**: name it (e.g. "Voxa Unlimited"), add a **recurring**
price, save, and copy the price id (`price_...`).

## 2. The webhook

**Developers → Webhooks → Add endpoint**:

- Endpoint URL: `https://voxa-zpukmw.fly.dev/api/billing/webhook`
- Events: `checkout.session.completed`, `customer.subscription.updated`,
  `customer.subscription.deleted`

Open the endpoint and reveal its **signing secret** (`whsec_...`).

## 3. The customer portal

**Settings → Billing → Customer portal**: activate it, and allow customers to cancel and to
update their payment method. This is where "Manage plan" sends subscribers.

## 4. The secrets

- `STRIPE_SECRET_KEY`: **Developers → API keys**, the secret key (`sk_test_...` / `sk_live_...`).
- `STRIPE_PRICE_ID`: from step 1.
- `STRIPE_WEBHOOK_SECRET`: from step 2.
- `SUPABASE_SERVICE_ROLE_KEY`: Supabase, **Project Settings → API Keys**, the `service_role`
  key. It can change any account, so keep it only in Fly's secrets. Voxa uses it for one
  thing: setting an account's plan after Stripe confirms a payment.
- `VOXA_PRICE_LABEL` (optional): what the button says, e.g. `$5 a month`.
- `VOXA_PUBLIC_URL`: `https://voxa-zpukmw.fly.dev`, where Stripe sends people back.

From a computer: `fly secrets set -a voxa-zpukmw NAME=value ...`. From an iPad: the Fly.io
dashboard, **your app → Secrets**, or ask the agent that deploys.

## 5. Check it

Signed in with a free account, the header shows **Upgrade**. Pay with the test card; back
on Voxa the page confirms the paid plan and the header shows **Manage plan**. `fly logs`
shows `Stripe checkout.session.completed ...: paid`. Cancelling in the portal returns the
account to free when the subscription ends.
