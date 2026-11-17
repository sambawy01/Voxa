# The Voxa agent API

Agents and scripts can speak text, make audiobooks and make two-host podcasts. It is the same
service as the website and uses the same plan: there is no separate bill. The live reference
is [`/api/openapi.json`](../server.py) (OpenAPI 3.1), [`/llms.txt`](../web/developers.html) and the
`/developers` page; this file explains how it is built.

## Keys

An API key belongs to one Voxa account and acts as that account (`apikeys.py`). Keys look like
`vx_live_…`, are shown once, and only a SHA-256 of each is stored. Up to five active keys per
account. Make, list and revoke them at `/developers`, or with `POST/GET /api/keys` and
`DELETE /api/keys/<id>`, which need a signed-in **session**, never a key, so a leaked key cannot
mint more keys.

A key has no tier of its own: the account's tier (Lite or Studio) is recorded when the account
signs in on the web and when Paddle's webhook changes it, so a key follows a new or cancelled
subscription. 120 requests a minute per account (`VOXA_API_RATE`).

Keys need accounts, so they exist only once sign-in is configured (docs/SUPABASE.md).

## Operations

| REST | MCP tool | What |
|---|---|---|
| `POST /api/v1/speech` | `speak` | text → MP3 or WAV, synchronous. REST answers with the audio; MCP returns an `audio_url`. |
| `POST /api/v1/audiobooks` | `make_audiobook` | `text` (chapters start at "Chapter N" lines) or `chapters`, → job; MP3 with chapter marks |
| `POST /api/v1/podcasts` | `make_podcast` | a document → DeepSeek writes the script → two Studio voices record it → job; MP3 with chapters, and the script |
| `GET /api/v1/jobs/<id>?wait_seconds=` | `get_job` | `queued`, `running`, `done`, `error`; when done `audio_url`, `chapters`, `duration_seconds` |
| `DELETE /api/v1/jobs/<id>` | `cancel_job` | stop a job (characters already used are not returned) |
| `GET /api/v1/voices?lang=` | `list_voices` | Studio voice ids with language, gender and class (public) |
| `GET /api/v1/account` | `get_account` | plan, characters and minutes left, podcast count |
| `GET /api/v1/files/<name>` | | the audio; the unguessable name is the secret, so it needs no key; kept 24 hours |
| `POST /mcp` | | MCP over streamable HTTP (JSON-RPC); `initialize` and `tools/list` need no key, `tools/call` does |

`agentapi.OPERATIONS` is the one table the OpenAPI file, llms.txt and the MCP tool list are built
from. The operations themselves are the `op_*` functions in `server.py`; REST routes and MCP
tools call the same ones, so they behave alike.

## Plan rules for agents

The on-device voices run only in a browser, so the API uses **Studio voices** and they count
against the plan exactly as they do on the site (plans.py):

- Characters are taken when a job starts (`WalletEmpty` → 429 with what is needed) and returned if
  the job fails. A cancelled job is not refunded.
- Lite: audiobooks 1 new book a day; podcasts 1 a week up to 5 minutes. Studio: no audiobook limit;
  podcasts 3 a day up to 20 minutes.
- A podcast checks the account can afford about half an episode before the script is written (so a
  wallet that is plainly too small fails fast and costs nothing). If the written script turns out
  to cost more than the account has, the job still finishes with the **script** and a `warning`,
  and nothing is charged.
- An account may have two unfinished jobs at once; the server runs three at a time
  (`VOXA_LONG_JOBS`), up to 20 waiting.

## How a long job runs (`longjobs.py`)

1. The text becomes chapters (`find_chapters`) and chunks of at most 1,500 characters (`split_chunks`),
   the same rules as `web/book.js`.
2. Each chunk goes through the ordinary speech queue (`speak_wait`), so the GPU router, the audio
   cache and the queue limits all apply, and repeated text is free.
3. Each chunk is encoded to MP3 (libsndfile, ~64 kbit/s) and appended to a file on disk; the ID3 tag
   with a `CHAP` mark per chapter is put in front at the end. Nothing holds a whole book in memory.
4. Files live in `$VOXA_DATA_DIR/outputs` for 24 hours (and at most 2 GB, oldest first).

Not in the first version: PDF or file upload (send text), custom pauses, per-line voices for
audiobooks, webhooks when a job finishes (poll, or use `wait_seconds`).

## Tests

`tests/test_longjobs.py` (chapters, chunks, ID3, building audio, the job runner) and
`tests/test_agent_api.py` (keys, speech, audiobooks, podcasts, limits, cancel, MCP, rate limits) run
without a speech model: generation and the script writer are stubbed. With `soundfile`
installed, one test also encodes a real MP3.
