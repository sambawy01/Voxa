# Headless mode: `voxa serve`

`voxa` is the command-line side of the Voxa app: the same voices, the same data folder
and the same licence, with no window. Use it on a home server or a GPU box, or to script
speech.

```sh
voxa import ./voxa-kokoro-pack     # or install voices in the app; both share the data folder
voxa voices
voxa say "Hello there." --voice harper -o hello.wav
voxa serve                         # http://127.0.0.1:7788, this machine only
voxa serve --lan --port 7788       # every network interface
```

The data folder is the app's own, `<OS data folder>/app.voxa.desktop`, or `--data <folder>`.
On a machine that needs a licence, run `voxa activate <key>` once. It counts as one of the
licence's devices, like the app.

## HTTP API

Every request except `/v1/health` needs the token. `voxa serve` prints it at start, and
it is kept in `<data>/serve-token`, readable only by its owner.

| Request | Response |
|---|---|
| `GET /v1/health` | `{"ok": true, "version": "0.1.0"}` |
| `GET /v1/voices` | the voice list, as in the app (cloned voices included) |
| `POST /v1/speech` `{"text": "…", "voice": "harper", "speed": 1.0}` | `audio/wav` (16-bit) |

```sh
TOKEN=$(cat ~/.local/share/app.voxa.desktop/serve-token)    # Linux
curl -H "Authorization: Bearer $TOKEN" -d '{"text":"Hello.","voice":"harper"}' \
     http://localhost:7788/v1/speech -o hello.wav
```

**Errors** are JSON `{"error": "…"}`:
- 400: bad request
- 401: missing or wrong token
- 403: licence needed
- 404: unknown voice
- 413: body over 64 KB

**Requests run one at a time**, because each one already uses the machine's cores.
**Audio from cloned voices** is watermarked and labelled, as in the app.

## Security

- Without `--lan`, only programs on this machine can connect.
- With `--lan`, anyone on the network who has the token can make speech with every
  installed voice, including your cloned ones.
- There is no TLS. Use it on networks you trust, or put it behind a TLS proxy.
