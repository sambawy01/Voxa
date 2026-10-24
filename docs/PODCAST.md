# Podcasts (DeepSeek)

`/podcast` turns a document into a two-host conversation. The page reads a PDF, a PowerPoint
(.pptx, including speaker notes) or text on the device and sends its text to Voxa. Voxa asks
DeepSeek to write a script and hands it back to the page. The person reads and edits the
script, then the page voices each turn with Voxa's own voices and makes one MP3 with chapter
marks. English can use the on-device voices, which cost nothing. Other languages use Studio
voices and the daily character allowance.

## How it fits together

- `podcast.py`: checks the request, builds the prompt, and calls
  `https://api.deepseek.com/chat/completions` (model `deepseek-flash`, JSON output, thinking
  off). It validates the script and strips stage directions, markdown and links so the
  voices read only speech. A failed or empty reply is retried once. Each script is written
  on its own thread (at most 4 at once), and the page polls for it. The document text is
  dropped from memory as soon as the script is written.
- `server.py`:
  - `POST /api/podcast/script` starts a script and returns `202 {id}`.
  - `GET /api/podcast/script/<id>` returns it, only to the person who asked.
  - `/api/config` includes `podcast` when the feature is on.
- `web/podcast.html` and `web/podcast.js`: the page, plus the deck reader and the script editor.
- Hosts: a woman and a man by default, or two women or two men. Each host's voice can be
  heard before choosing:
  - Studio voices: `GET /api/preview/<voice>` speaks a fixed line in the voice's language. It
    is made once per voice, free, and kept in `<data dir>/previews/`, so it survives
    restarts with a Fly volume.
  - On-device voices: the line is spoken in the browser.
- The same button (`web/preview.js`) is on every voice picker: the main page, audiobooks and podcasts.

## Cost

Each script is one DeepSeek call. Prices for `deepseek-flash` per million tokens: input
$0.15 off-peak or $0.30 peak; output $0.60 off-peak or $1.20 peak. A 30-page deck (about
20,000 tokens) and a 10-minute script (about 3,000 tokens) cost about 1 cent at peak and half
that off-peak. A document at the 200,000-character limit with a 20-minute script costs about
2 cents.

## Who can make podcasts

The counts are in `plans.py` (`PODCAST_RULES`): Voxa Lite makes 1 script a week, up to 5
minutes; Voxa Studio 3 a day, up to 20 minutes. Visitors who are not signed in cannot make
podcasts once sign-in is set up; until then they count as Voxa Lite. A script that fails is
refunded. The audio uses on-device voices, or Studio voices from the wallet and allowances.

## Switch it on

1. In the DeepSeek platform (platform.deepseek.com), add credit and create an API key.
2. Give it to the server as a secret. Never put it in the repository or in a chat:

   ```bash
   fly secrets set -a voxa-zpukmw DEEPSEEK_API_KEY=<the key>
   ```

   Or use the Fly dashboard: the app, then **Secrets**, then **New secret**.
3. After the restart, `fly logs` shows `podcasts on (deepseek-flash)`. The page at `/podcast`
   then shows the upload step instead of "Not available yet".

Optional variables:

- `VOXA_PODCAST_MODEL`, for example `deepseek-v4-pro` for stronger but slower and pricier scripts.
- `DEEPSEEK_API_BASE`, for a proxy or a test server.

## Privacy

The document's text goes to DeepSeek, whose servers are in China. The page says so next to
the button that writes the script. The privacy policy and terms pages cover it, but have a
lawyer review them before launch, especially for EU and UK users. Use a different provider
if that is not acceptable for your users.
