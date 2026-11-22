# The Voxa API for AI agents

Voxa has an API that AI agents (Claude and others) can use to make speech, podcasts and
audiobooks: a **REST API** under `/v1/` and an **MCP server** at `/mcp`. Both offer the same
things, from the same code (`agentapi.py`).

The website's voices run in the visitor's browser. An agent calls from its own servers, so
here **Voxa's server makes the audio**, with:

- **Supertonic 3** (Supertone, OpenRAIL-M): 31 languages, ten voices (`lina-a`, `adam-a`,
  `amira-ar`, ...), about 4-5x faster than real time per CPU core.
- **Kokoro-82M** (hexgrad, Apache-2.0): the Studio catalogue (`harper`, `henry`, ...).

## Connect Claude

The server is `https://voxa-zpukmw.fly.dev/mcp` (or your domain + `/mcp`).

- **Claude (web or desktop app):** Settings → Connectors → Add custom connector. URL:
  `https://voxa-zpukmw.fly.dev/mcp?key=YOUR_KEY` (the key in the URL, because custom
  connectors do not take headers).
- **Claude Code:**
  `claude mcp add --transport http voxa https://voxa-zpukmw.fly.dev/mcp --header "Authorization: Bearer YOUR_KEY"`

Then ask, for example: *"Use Voxa to make a 2-minute podcast about coffee with two hosts,
Lina and Adam, and give me the MP3 link."*

### Tools

| Tool | What it does |
|---|---|
| `list_languages` | Languages and how many voices each has |
| `list_voices` | Voices, filtered by language, engine or gender |
| `make_speech` | Read up to 3,000 characters with one voice |
| `create_podcast` | A multi-voice episode from a script (`Name: line`, `#`/`##` headings) or a list of lines; a voice per speaker; pause length |
| `create_audiobook` | A book or document with one voice; chapters found at "Chapter 1", "Part Two" or markdown headings, or given as a list |
| `get_job` | Progress; when done, MP3/WAV links, duration and chapter marks; with `script=true`, every numbered line |
| `list_jobs` | Recent jobs and characters used today |
| `edit_line` | New wording or voice for one line; only that line is made again |
| `cancel_job` | Stop a job |

Jobs return at once with an id; the agent polls `get_job` until `status` is `done`. Audio
links are signed (`?token=`), so they work without the key and can be given to anyone.

## REST

Send `Authorization: Bearer YOUR_KEY`. `GET /v1/docs` (no key) lists everything.

```bash
KEY=YOUR_KEY; B=https://voxa-zpukmw.fly.dev
curl -H "Authorization: Bearer $KEY" "$B/v1/voices?language=a&engine=supertonic"
curl -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' $B/v1/podcasts -d '{
  "script": "# Coffee Talk\nLina: Welcome!\nAdam: Thanks, Lina.",
  "voices": {"Lina": "lina-a", "Adam": "adam-a"}}'
curl -H "Authorization: Bearer $KEY" "$B/v1/jobs/JOB_ID?script=1"
curl -H "Authorization: Bearer $KEY" -X PATCH $B/v1/jobs/JOB_ID/lines/1 -d '{"text": "Thanks so much, Lina!"}'
curl -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' $B/v1/speech \
     -d '{"text": "Hello!", "voice": "lina-a"}' -o hello.wav
```

## Keys and limits

- Keys are set in `fly.toml` as `VOXA_API_KEYS = "name:sha256,name2:sha256"`. Only the SHA-256
  of a key is stored; the key itself is given once to whoever uses it. Make one with:
  `python3 -c "import hashlib,secrets;k='vx_'+secrets.token_urlsafe(32);print(k, hashlib.sha256(k.encode()).hexdigest())"`
  To revoke a key, remove its entry and deploy.
- Each key may use `VOXA_API_DAILY_CHARS` characters per UTC day (300,000 by default). Failed
  speech is refunded.
- One job runs at a time on the server; others wait in line.
- **Storage:** finished audio is kept for 7 days, but the Fly app has no volume yet, so a deploy
  or restart clears jobs and changes the link signature. Download the audio when a job is done.
  For lasting links, add a Fly volume mounted at `/data` (and optionally `VOXA_API_SECRET`).
- Voices are synthetic. Supertonic's OpenRAIL-M licence forbids impersonation and presenting
  its speech as not machine-generated; agents should tell listeners it is AI.
