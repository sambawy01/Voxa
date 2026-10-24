# Voxa

Text to speech, served properly. Nine languages, Apache-2.0 model, cached audio.

**Live:** https://voxa-zpukmw.fly.dev

## Why a server and not the browser

The first version ran Kokoro in the browser via `kokoro-js`. It was abandoned for two
measured reasons:

1. **The browser build is English-only.** Its bundled espeak build ships just `["en"]`
   (`af=["en"]` in `kokoro.web.js`), so the 8-language catalogue in the model card is
   unreachable in the browser — non-English requests fail or produce accented nonsense.
2. **It is far too slow on a phone.** ~100 MB download plus single-threaded wasm (Safari
   has no cross-origin isolation on GitHub Pages) meant minutes per sentence, with no
   progress feedback. It never finished in testing.

The Python runtime fixes both: all nine languages work, and generation is ~2x realtime on
the Fly machine's two shared CPUs.

The old browser build's files (`src/`, `index.html`, `vite.config.js`, `package.json`) are
still in the repo but no longer deployed: GitHub Pages now serves `pages/index.html`, which
redirects to the Fly app.

## Architecture

```
server.py        Python stdlib HTTP server + Kokoro-82M
                 - model loads ONCE at boot and stays in memory
                 - one worker thread runs every generation, in order (a job queue), so
                   memory never stacks and no request holds a connection open for minutes
                 - generated audio cached by sha256(text|voice|speed|lang), LRU, 256 MB cap
                 - multi-sentence text is generated per sentence then joined into one WAV
Dockerfile       python:3.11-slim + espeak-ng + CPU torch + zh/ja G2P; model and all 54
                 voices baked in at build time, runs with HF_HUB_OFFLINE=1
fly.toml         4 GB shared-cpu-2x, one machine, fra region, health check on /health
tests/           unit tests, generation stubbed, no torch needed
scripts/smoke.py end-to-end check of a running server: all nine languages + bad input
pages/           GitHub Pages redirect to the Fly app
```

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | the web UI |
| GET | `/health` | `{ok, pipelines, cached}` |
| GET | `/api/languages` | language code → label |
| GET | `/api/voices` | available Kokoro voices per language |
| POST | `/api/jobs` | `{text, lang, voice, speed}` → job `{id, status, progress, ahead?}` |
| GET | `/api/jobs/<id>` | job status; when `done` it carries `audio_url` |
| GET | `/api/audio/<key>` | the finished `audio/wav` |
| POST | `/api/tts` | `{text, lang, voice, speed}` → `audio/wav`, waits up to 300 s |

The page uses the job endpoints: submit, poll progress, then fetch the audio. `/api/tts` is
the same queue in one call, for scripts; its `X-Voxa-Cache: hit|miss` header says whether
the audio was cached.

Request fields:

- `text` — required string, at most 4,000 characters.
- `lang` — optional, default `a`. Unknown codes are a 400.
- `voice` — optional, defaults to the language's first voice. A Voxa voice id from
  `/api/voices` (e.g. `harper`), which must belong to that language; anything else is a 400.
  Kokoro's own ids (e.g. `af_heart`) are still accepted and map to the matching Voxa voice.

Voices are defined in `voices.json`: a Voxa name, gender, accent, Kokoro's published quality
grade, and the Kokoro voice behind it, or a weighted blend of same-language Kokoro voices
(the "signature" voices, marked ✦ on the page). Rename or add voices there; the server checks
the file at startup (unique ids, one language per blend, weights adding up to 1).
- `speed` — optional number, default 1.0, must be 0.5–2.0. Audio length and memory scale
  with 1/speed: `0.01` once took 4.6 GB and OOM-killed the machine.

## Plans, the wallet and accounts

The rules live in `plans.py`. On-device voices are unlimited for everyone. Accounts use
Studio voices from a **wallet** (the one-time welcome credit and HD packs, which never
expire); Voxa Studio adds a monthly HD allowance (used before the wallet) and Standard
voices under a monthly fair-use limit. Voxa Lite makes 1 new audiobook a day and 1 podcast a
week (up to 5 minutes); Studio has no audiobook limit and 3 podcasts a day (up to 20).
Monthly allowances reset on the 1st (UTC). Replaying cached audio is free and failed
generations are refunded.

| Variable | Default | Meaning |
|---|---|---|
| `VOXA_WELCOME_CHARS` | 10000 | one-time welcome credit per account (about 11 minutes) |
| `VOXA_WELCOME_PER_NETWORK_DAY` | 3 | welcome credits one network address may collect a day; one per device |
| `VOXA_HD_MONTHLY_CHARS` | 300000 | HD characters a month on Voxa Studio |
| `VOXA_STANDARD_MONTHLY_CHARS` | 3000000 | Standard-voice fair use a month on Voxa Studio |
| `PADDLE_PRICE_HD100` / `_HD300` / `_HD600` | (none) | Paddle price ids of the HD packs; a pack is sold once its id is set |
| `VOXA_FREE_DAILY_CHARS` | 1500 | Studio characters a day for visitors, only while sign-in is off |

Sign-in uses Supabase. Setting `SUPABASE_URL` and `SUPABASE_ANON_KEY` turns it on: requests
with `Authorization: Bearer <access token>` are counted against that account, and an
invalid or expired token is a 401 (never a silent fall back to guest). Tokens signed with
the project's asymmetric keys (ES256/RS256) are checked against its JWKS; set
`SUPABASE_JWT_SECRET` as well for projects still on the legacy HS256 secret. An account is
paid when its `app_metadata.tier` is `"paid"`; `user_metadata` is ignored because users can
edit it. `GET /api/config` tells the page whether sign-in is on, and `GET /api/quota`
returns today's allowance and the signed-in account.

Bad input is always a JSON 400, never a dropped connection. When 16 jobs are already waiting
the server answers 503 with `Retry-After`.

Language codes: `a` US English, `b` UK English, `e` Spanish, `f` French, `h` Hindi,
`i` Italian, `j` Japanese, `p` Brazilian Portuguese, `z` Mandarin.

## Measured

On a 24-core dev box:

| | |
|---|---|
| Pipeline load | 21.6 s (once, at boot) |
| 4 sentences (9.3 s audio), cache miss | 1.79 s |
| Same request, cache hit | 0.0014 s |

On a 4-core box: 4,000 characters (268 s of audio) took 131 s. Peak memory with all nine
languages loaded was 2.9 GB. On Fly's shared CPUs, two short sentences took 4–27 s.

## Notes and limits

- **English only was never true of the model** — only of the deprecated browser build.
- **French has exactly one voice** (`ff_siwis`), Spanish three. That is the model; the
  UI says so rather than pretending otherwise.
- The cache and the job table are in-memory, so they reset when the machine restarts (the
  page then says so and asks you to generate again). Run ONE machine: a job submitted to
  one machine cannot be polled on another.
- Each language's pipeline loads lazily on first use, sharing the one model.
- Japanese uses fugashi with the small `unidic-lite` dictionary rather than `misaki[ja]`,
  which pulls the 770 MB `unidic` and then fails to start without its separate download.

## Local development

```bash
python -m venv .venv && . .venv/bin/activate
pip install --index-url https://download.pytorch.org/whl/cpu torch
pip install "kokoro>=0.9.4" soundfile numpy "misaki[zh]" \
    fugashi jaconv mojimoji pyopenjtalk unidic-lite   # pyopenjtalk needs cmake + a C compiler
apt-get install espeak-ng          # required for G2P in every language
python server.py                   # http://localhost:8080

# On-device voices (optional): browser packages, and Hojo TTS Light repacked for the browser
python scripts/fetch_vendor.py
pip install numpy onnx onnxruntime && python scripts/build_hojo.py   # ~126 MB into static/

python -m unittest discover -s tests          # fast, no model needed
python scripts/smoke.py http://localhost:8080 # end to end, real model
```

## Deploy

```bash
fly deploy
python scripts/smoke.py            # checks the live app
```

The model is downloaded during the image build so containers start without a fetch.
