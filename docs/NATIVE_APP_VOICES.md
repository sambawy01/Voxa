# Voxa native app: voices, models and how they stay up to date

**Status: proposal.** Licences were checked against model cards and LICENSE files on
2026-11-02. Speed and memory requirements are **not measured yet**: every "requirement" below
is a target to confirm on real devices before it goes on a voice card.

The app is a one-time purchase that runs everything on the device. Models download inside the
app after purchase. Nothing is uploaded.

## Device labels

Every voice card shows one of these, plus its measured numbers.

| Label | Means | Target hardware |
|---|---|---|
| 📱 **Phone** | Runs on a phone, offline | iPhone 12+ / mid-range Android 2022+, 4 GB RAM |
| 💻 **Computer** | Needs a laptop or desktop; no graphics card needed | Apple Silicon Mac, or Windows/Linux with a modern CPU, 8 GB RAM |
| 🖥️ **GPU** | Needs a strong graphics chip; also runs headless on a home server | NVIDIA 8–16 GB VRAM, or Apple M Pro/Max with 16 GB+ |

Each card also lists: languages, download size, speed on 3 reference devices (measured),
voice cloning yes/no, licence and required credit, and "works offline ✓".

## Hard licensing rules (they decide the catalogue)

1. **Weights must allow commercial use in a sold app.** Excluded: F5-TTS, XTTS-v2, Fish
   Speech / OpenAudio, Spark-TTS (non-commercial). Also excluded: VibeVoice (its card says
   research only, and Microsoft has pulled the larger models), Higgs TTS (Llama-based licence
   that requires a separate deal above 100k users), Orpheus (built on Llama 3.2, so Llama
   terms apply), KittenTTS 2 (licence ends above $1M revenue or funding) and IndexTTS-2
   (custom licence that says to contact bilibili for commercial use).
2. **No espeak-ng in the App Store build.** espeak-ng is GPL-3.0. That conflicts with App Store
   terms and would make a closed app GPL. Consequences:
   - Kokoro ships for **English, Japanese and Chinese** only, since those don't need espeak.
   - Spanish, French, Italian, Portuguese and Hindi come from models that take plain text:
     Supertonic, Pocket TTS, Qwen3-TTS, Chatterbox Multilingual.
   - Piper is out: its code is now GPL-3.0, and its voices inherit their datasets' licences.
3. **Licences with conditions are passed on in Voxa's terms.**
   - Supertonic (OpenRAIL-M): its use restrictions must flow down to users.
   - Pocket TTS and Kyutai (CC-BY-4.0): attribution required.
   - NVIDIA RE-USE (OpenMDW-1.1): keep the licence text and notices.
   - Every credit goes on the Credits screen.
4. **Pin and mirror every model.** Upstream repos can disappear (VibeVoice did) or change
   licence (Fish did, on 2026-10-13). Voxa hosts its own copy of every version it ships.

## Catalogue v1

### 📱 Phone

| Voice family | Model | Size | Languages | Cloning | Role |
|---|---|---|---|---|---|
| **Voxa Instant** | Kitten TTS 0.8 nano, 15M, Apache-2.0 | ~24 MB | English, 8 voices | No | Oldest and cheapest phones; instant start |
| **Voxa Classic** | Kokoro-82M, int8, Apache-2.0 | ~90 MB | US/UK English, Japanese, Chinese | No | Flagship. Today's Voxa voices (Harper, Atlas, the ✦ blends) carry over |
| **Voxa World** | Supertonic 3, ~99M, OpenRAIL-M | ~400 MB fp32 (quantise) | 31 languages | Presets only | Every other language on a phone |
| **Voxa Europe** (alternative) | Kyutai Pocket TTS 100M (no-cloning build), CC-BY-4.0 | TBD | en, fr, de, pt, it, es | No (no-cloning build) | Compare against Supertonic; keep the better one per language |
| Voxa Lite (existing) | Hojo TTS Light 40M, Apache-2.0 | ~126 MB | English, Chinese | No | Already shipping on web; retire if Kitten/Kokoro beat it |

Clean-up on the phone: **GTCRN** (MIT, 48K params) for voice memos and **DeepFilterNet 3**
(MIT/Apache) for stronger noise.

### 💻 Computer

| Voice family | Model | Languages | Cloning | Role |
|---|---|---|---|---|
| **Voxa Narrator** | Soprano 1.1-80M, Apache-2.0 (~280 MB) | English | No | Whole audiobooks fast on a CPU (published: 20× real time) |
| **Voxa Expressive** | Chatterbox Nano 110M / Flash, MIT, built-in watermark | English | Yes | Emotional delivery; your own voice |
| **Voxa Polyglot** | MOSS-TTS-Nano-100M, Apache-2.0 | ~20 languages | Yes | Cloning in many languages on a CPU (new in 2026; evaluate) |
| **Voxa Studio Lite** (Mac) | Marvis TTS 250M, Apache-2.0 (MLX 4-bit ~414 MB) | en, fr, de | Yes | Apple Silicon only |

Clean-up on the computer: DeepFilterNet 3. **RE-USE (film dialogue)** on Apple Silicon
through Metal: a community port reports 2.86× real time on M5. It is too slow on a plain CPU
(0.17× real time), so on Windows it needs the GPU tier.

### 🖥️ GPU (desktop or headless server)

| Voice family | Model | Languages | Cloning | Role |
|---|---|---|---|---|
| **Voxa Studio** | Qwen3-TTS 1.7B, Apache-2.0 (0.6B for smaller GPUs) | zh, en, ja, ko, de, fr, ru, pt, es, it | Yes, plus "describe a voice" | Premium flagship |
| **Voxa Studio World** | Chatterbox Multilingual V3 500M, MIT, watermark | 23 languages | Yes | Emotion control in many languages |
| **Voxa Dialogue** | Dia2 2B, Apache-2.0 | English | Yes | Two-host podcasts with natural back-and-forth |
| **Voxa Mandarin+** | Fun-CosyVoice3 0.5B, Apache-2.0 | 9 languages + 18 Chinese dialects | Yes | Chinese dialects |
| (evaluate) | VoxCPM2 2B, Apache-2.0, 48 kHz | 30 languages | Yes | High-fidelity alternative to Qwen3 |

Clean-up on the GPU: **RE-USE** (OpenMDW-1.1, commercial OK) for film dialogue.

**Headless mode:** the desktop app also runs as `voxa serve` on a home server or GPU box, and
other devices on the same network can use it. It's the same licence and still nothing leaves
your network.

### Runtimes

- **sherpa-onnx 2.x** (Apache-2.0): Phone tier and CPU denoisers on iOS, Android, Windows,
  Linux and macOS. It supports Kokoro, Kitten, Pocket TTS, Supertonic and GTCRN, and has no
  espeak since 2.0.
- **mlx-audio** (MIT): Apple Silicon Computer and GPU tiers. It supports Kokoro, Kitten,
  Qwen3-TTS, Chatterbox, Soprano, Marvis, VoxCPM2, MOSS-Nano, DeepFilterNet and RE-USE.
- **PyTorch / CUDA worker**: GPU tier on Windows/Linux, and headless mode.

## Voice cloning policy

Cloning runs on the device, so Voxa cannot moderate it afterwards. Rules:

- Clone only from a live recording of a short consent sentence spoken in the app, not from an
  uploaded file of someone else.
- Prefer models that watermark their output (Chatterbox has a built-in watermark).
- The terms forbid impersonation.

Decide before launch whether cloning ships in v1.

## Keeping the models updated

### How the app gets models

- The app reads a **signed catalogue file** (`catalogue.json`) from Voxa's servers. Each entry
  holds: voice id, model, version, file hashes, size, device label, measured requirements,
  licence and credit text, and the minimum app version.
- Models are downloaded on demand from **Voxa's own mirror**, never straight from upstream,
  and checked against their hashes.
- New voices for an architecture the app already runs are **data, not code**, so they arrive
  without an App Store update. A new architecture needs an app update.

### Pipeline for each candidate model

1. **Scout (monthly).** Check Hugging Face trending and the sherpa-onnx and mlx-audio release
   notes. Those runtimes adding a model is the best signal that it runs on devices.
2. **Licence gate.** Weights and code allow commercial use in a sold app, no GPL in the App
   Store build, and conditions recorded. Fail means stop.
3. **Quality gate (automated).** Run a fixed test text per language:
   - intelligibility: transcribe with Whisper and compute word error rate
   - naturalness: UTMOS score
   - a pronunciation list per language: numbers, dates, names
   - it must beat or match the voice it would replace.
4. **Speed gate.** A device lab: an old phone, a new phone, an Apple Silicon Mac, a Windows
   laptop on CPU only, and an NVIDIA box. The results become the label and numbers on the card.
5. **Listening review** by a person, per language.
6. **Beta channel**, then release in the next quarterly catalogue update.

### Rules that keep the "works forever" promise

- **Downloaded models are never deleted or swapped silently.** An update offers the new
  version, and the old one stays until the user removes it.
- **Voice names are stable.** "Harper" moves to a newer model only if it still sounds like
  Harper. Otherwise the newer model ships as a new voice.
- **If an upstream model is pulled or relicensed**, copies already downloaded keep working:
  Apache, MIT and CC-BY grants are irrevocable for what was received. Voxa stops offering new
  downloads and says so in the catalogue.
- Cadence: quarterly catalogue releases, with fixes at any time.

### Business tie-in (proposal)

- The purchase includes every catalogue voice the installed app version can run, forever.
- Major app versions, which add new architectures and features, are an optional discounted
  upgrade.

## First steps

1. Build the device-lab benchmark for the 📱 tier (Kitten nano, Kokoro int8, Supertonic 3,
   Pocket TTS) on sherpa-onnx, on 3–5 real phones.
2. Prototype `catalogue.json` and the download, hash-check and keep-old-versions flow.
3. Run listening tests of Supertonic and Pocket TTS for es, fr, it, pt and hi against today's
   espeak-based Kokoro voices.
