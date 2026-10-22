# Voxa privacy policy (DRAFT)

> **Draft for review, not legal advice.** Written from what the code does on the branches up to
> `feat/phase-7-legal`. Before publishing: add the operator's legal name and contact address,
> the governing law, and have it reviewed for the countries Voxa serves (e.g. GDPR in the EU/UK).
> Update it whenever the data below changes.

## What stays on your device

- **Books you open for audiobooks.** PDFs and text files are read in your browser. The file
  itself is never uploaded.
- **Audiobook progress and audio.** Saved in your browser's storage (IndexedDB) until you
  delete it ("Delete saved audio for this book") or clear your browser data.
- **On-device voices.** Speech made with on-device voices is generated in your browser and
  never sent to Voxa.

## What Voxa's servers receive

- **Text you ask a Studio voice to speak**, with the language, voice and pacing. It is used to
  make the audio and is not stored in a database. The finished audio is kept in the server's
  memory (up to 256 MB in total, oldest first) so repeated requests come back quickly, and is
  lost when the server restarts. Server logs record a request's language, voice and length when
  it fails, not its text.
- **Paid accounts' audiobook text** is processed the same way on a GPU service run by Modal
  (when configured).
- **An anonymous id** in a cookie (`voxa_id`, one year) and **your IP address**, to apply the
  daily character allowance. Usage counts are kept per day and deleted the next day.
- **Your account** (when sign-in is on): your email address, held by Supabase, which runs
  sign-in. Usage is counted by account.
- **Payments** (when the paid plan is on): Stripe handles card details; Voxa never sees them.
  Voxa stores your Stripe customer and subscription ids and your plan on your account, and its
  logs record plan changes by account id.

## Third parties

| Service | What it receives | Why |
|---|---|---|
| Fly.io | everything sent to Voxa's server (hosting) | runs Voxa |
| Supabase | your email and sign-in sessions | accounts |
| Stripe | payment and billing details | the paid plan |
| Modal | paid accounts' Studio text | GPU voices |
| Hugging Face | your IP address, when your browser downloads the on-device voice model | on-device voices |
| jsDelivr | your IP address, when the page loads the sign-in library | sign-in |

## Your choices

- Use on-device voices to keep text off Voxa's servers entirely.
- Delete saved audiobooks from the audiobook page, or clear your browser data.
- To delete your account and its data, contact: **[contact address]**.

## Changes

We will post changes here with the date. Last updated: **[date]**.
