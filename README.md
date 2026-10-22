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

The Python runtime fixes both: all languages work, and generation is ~4x realtime on CPU.

## Architecture

```
server.py        Python stdlib HTTP server + Kokoro-82M
                 - model loads ONCE at boot and stays in memory
                 - generated audio cached by sha256(text|voice|speed|lang)
                 - multi-sentence text is generated per sentence then joined into one WAV
Dockerfile       python:3.11-slim + espeak-ng + CPU torch; model baked in at build time
fly.toml         2 GB shared-cpu-2x, fra region, health check on /health
```

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | the web UI |
| GET | `/health` | `{ok, pipelines, cached}` |
| GET | `/api/languages` | language code → label |
| GET | `/api/voices` | available Kokoro voices per language |
| POST | `/api/tts` | `{text, lang, voice, speed}` → `audio/wav` |

Response header `X-Voxa-Cache: hit|miss` tells you whether the audio was cached.

Language codes: `a` US English, `b` UK English, `e` Spanish, `f` French, `h` Hindi,
`i` Italian, `j` Japanese, `p` Brazilian Portuguese, `z` Mandarin.

## Measured on a 24-core dev box

| | |
|---|---|
| Pipeline load | 21.6 s (once, at boot) |
| 4 sentences (9.3 s audio), cache miss | 1.79 s |
| Same request, cache hit | 0.0014 s |

## Notes and limits

- **English only was never true of the model** — only of the deprecated browser build.
- **French has exactly one voice** (`ff_siwis`), Spanish three. That is the model; the
  UI says so rather than pretending otherwise.
- The cache is in-memory, so it resets when the machine restarts. Move to a volume or
  object store if it needs to survive.
- Voxes are per-language pipelines; each loads lazily on first use.

## Local development

```bash
python -m venv .venv && . .venv/bin/activate
pip install --index-url https://download.pytorch.org/whl/cpu torch
pip install "kokoro>=0.9.4" soundfile numpy
apt-get install espeak-ng          # required for G2P in every language
python server.py                   # http://localhost:8080
```

## Deploy

```bash
fly deploy
```

The model is downloaded during the image build so containers start without a fetch.
