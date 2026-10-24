# Turning on accounts (Supabase)

Accounts are built and tested; they switch on when the server has a Supabase project to
check sign-in tokens against. Until then everyone is a guest with Voxa Lite's 1,500 Studio characters a day; once it is on, visitors must sign in (free) to generate.

## 1. Create the project

1. At https://supabase.com create a project (the free plan is enough). Pick a region near
   the Fly app (`fra`, Frankfurt).
2. **Authentication → Sign In / Providers → Email**: keep it enabled. "Confirm email" can
   stay on: the sign-in link confirms the address.
3. **Authentication → URL Configuration**:
   - Site URL: `https://voxa-zpukmw.fly.dev`
   - Redirect URLs: add `https://voxa-zpukmw.fly.dev/`
4. Optional, **Google**: in Google Cloud create an OAuth client ("Web application") with the
   authorised redirect URI Supabase shows on its Google provider page, then paste the
   client id and secret into **Authentication → Sign In / Providers → Google**. Without
   this the page says Google sign-in is not available and email links still work.

## 2. Give the keys to the server

From **Project Settings → API** (or **API Keys**), copy the project URL and the public
**anon** key. Both are public by design: the page uses them to sign people in.

```bash
fly secrets set -a voxa-zpukmw \
  SUPABASE_URL=https://<project-ref>.supabase.co \
  SUPABASE_ANON_KEY=<anon key>
```

Projects created recently sign tokens with asymmetric keys, which the server fetches from
the project itself, so nothing else is needed. If the project still uses the legacy shared
JWT secret (**Project Settings → JWT Keys** says "Legacy JWT Secret"), also set
`SUPABASE_JWT_SECRET`. Never put the `service_role` key anywhere in Voxa.

Setting secrets redeploys the app. Guests then drop to 1,000 characters a day and the page
shows **Sign in**.

## 3. Check it

1. Open the site, tap **Sign in**, enter your email, open the link on the same device.
2. The header shows your email, and the allowance reads "1,500 of 1,500".
3. `fly logs` shows `sign-in enabled via https://<project-ref>.supabase.co` at startup.

## Making an account paid

Until payments exist (Phase 6), mark an account paid by hand in **SQL Editor**:

```sql
update auth.users
set raw_app_meta_data = raw_app_meta_data || '{"tier": "paid"}'
where email = 'someone@example.com';
```

The change applies the next time that person's token refreshes (within an hour) or when
they sign in again. `user_metadata` is deliberately ignored: users can edit it themselves.
