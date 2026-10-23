# Voxa privacy policy (DRAFT)

> **Draft for review, not legal advice.** Written from what the code does on the branches up to
> `feat/podcasts`. The DeepSeek row needs particular review: an international transfer to
> China under GDPR/UK GDPR, and DeepSeek's current API data terms. Before publishing: add the operator's legal name and contact address,
> the governing law, and have it reviewed for the countries Voxa serves (e.g. GDPR in the EU/UK).
> Update it whenever the data below changes.

## What stays on your device

- **Books you open for audiobooks.** PDFs and text files are read in your browser. The file
  itself is never uploaded.
- **Audiobook progress and audio.** Saved in your browser's storage (IndexedDB) until you
  delete it ("Delete saved audio for this book") or clear your browser data.
- **Podcast scripts and audio.** Saved in your browser's storage until you delete them from
  the podcast page or clear your browser data.
- **On-device voices.** Speech made with on-device voices is generated in your browser and
  never sent to Voxa.

## What Voxa's servers receive

- **Text you ask a Studio voice to speak**, with the language, voice and pacing. It is used to
  make the audio and is not stored in a database. The finished audio is kept in the server's
  memory (up to 256 MB in total, oldest first) so repeated requests come back quickly, and is
  lost when the server restarts. Server logs record a request's language, voice and length when
  it fails, not its text.
- **Documents you turn into podcasts.** The page sends the document's *text* (not the file),
  your focus note and the episode settings to Voxa, which passes them to DeepSeek to write the
  script and returns the script to your browser. Voxa does not store the text or the script;
  it is dropped from memory once the script is written, and finished scripts expire from
  memory within an hour. Server logs record a script's length, duration and language, not its
  content. Usage counts (scripts per day) are kept like the character allowance. DeepSeek
  processes the text on servers in the People's Republic of China under its own terms and
  privacy policy, which may allow it to keep and use API inputs.
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
| DeepSeek (China) | the text of documents you turn into podcasts, and your focus note | writing podcast scripts |
| Hugging Face | your IP address, when your browser downloads the on-device voice model | on-device voices |
| jsDelivr | your IP address, when the page loads the sign-in library | sign-in |

## Your choices

- Use on-device voices to keep text off Voxa's servers entirely.
- Don't use the podcast feature for confidential documents, or documents you may not share
  with a third party.
- Delete saved audiobooks from the audiobook page, or clear your browser data.
- To delete your account and its data, contact: **[contact address]**.

## Changes

We will post changes here with the date. Last updated: **[date]**.
