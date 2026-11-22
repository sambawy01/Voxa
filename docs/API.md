# The Voxa API for AI agents

AI agents (Claude and others) can use Voxa in three ways, all from the same code (`agentapi.py`):

| | Where the voices run | Key | Gives |
|---|---|---|---|
| **Player in the chat** (`play_on_device`) | the user's browser, inside the Claude chat | none | plays at once; Save MP3 |
| **Voxa on your computer** (local MCP server) | the user's computer | none | MP3 files in a folder |
| **Voxa's server** (MCP at `/mcp`, REST at `/v1/`) | Voxa's server | API key | MP3/WAV links to share |

## Player in the chat: free, on the user's device

The MCP server has two tools that need no key and cost the server nothing:

- `play_on_device`: give it a podcast `script` ("Name: line" turns, `#`/`##` headings) or a
  `text`. Claude shows the **Voxa player** in the chat (an [MCP App](https://github.com/modelcontextprotocol/ext-apps)):
  speaker cards that light up as each one talks, the words on screen, Play/Pause, a clickable
  script, and Save MP3. The player is the page `/player`, framed in the chat, so it runs the
  website's own on-device engines; nothing is sent to a server. Voices are chosen for each
  speaker (Wren and Calvin in English) unless the agent picks them; `style` is `steady`,
  `natural` or `expressive`. Up to 60,000 characters.
- `list_device_voices`: the on-device voices by language (desktop-only ones are marked).

Chat apps that do not show MCP Apps get a link instead that opens the same player on Voxa.
Some chat apps block downloads inside the chat; the player's **Open in Voxa** button opens it
in a browser tab, where Save MP3 works.

Connect it like the server below, with or without a key (without one, only these two tools work):
`https://voxa-zpukmw.fly.dev/mcp`.

## Voxa on your computer (Claude Desktop, Claude Code)

The same MCP server runs on your own computer: `agentapi.py` is served as
`https://voxa-zpukmw.fly.dev/voxa-mcp.py`, and [uv](https://docs.astral.sh/uv/) runs it
with what it needs (Python, numpy, onnxruntime, an MP3 encoder). On first use it downloads
Supertonic 3 from Voxa (111 MB, checked against SHA-256 hashes) to `~/.voxa`; after that it
works offline. Finished audio is saved as MP3 in `~/Music/Voxa` (or `~/Voxa`).

1. Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS, Linux) or
   `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` (Windows).
2. Check it works: `uv run https://voxa-zpukmw.fly.dev/voxa-mcp.py --check` says one sentence
   into `~/Music/Voxa/Voxa check.mp3`.
3. Connect Claude:
   - **Claude Code:** `claude mcp add voxa -- uv run https://voxa-zpukmw.fly.dev/voxa-mcp.py`
   - **Claude Desktop:** Settings → Developer → Edit Config, and add:
     ```json
     {"mcpServers": {"voxa": {"command": "uv", "args": ["run", "https://voxa-zpukmw.fly.dev/voxa-mcp.py"]}}}
     ```
     On macOS, if Claude Desktop cannot find `uv`, use its full path (`which uv`, usually
     `/Users/you/.local/bin/uv`).

Options: `--out FOLDER` (or `VOXA_OUT`) for where audio is saved; `--home` for models and
jobs. Running from the URL fetches the latest script each time Claude starts it; to pin a
version, download the file and run `uv run voxa-mcp.py` instead.

Locally there is no key and no daily limit. The tools are the server's (below), plus
`open_audio` (plays a finished file in the computer's default player); finished jobs give
`files` (paths on the computer) instead of links. Voices are Supertonic 3's ten, in 31
languages; the Studio (Kokoro) voices are server-only. `play_on_device` works here too.

## Voxa's server

An agent calling Voxa's server gets the audio made there, with:

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
| `play_on_device` | No key. The Voxa player in the chat, making the audio in the user's browser |
| `list_device_voices` | No key. The on-device voices, by language |
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

Send `Authorization: Bearer YOUR_KEY`. `GET /v1/docs` (no key) lists everything. `GET /v1/local`
(no key) is what the local server downloads.

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
