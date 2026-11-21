# Voxa native app: voices, models and how they stay up to date

**Status: proposal; cloning and purchase model decided 2026-11-02.** Licences were checked against model cards and LICENSE files on
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
   research only, and Microsoft has pulled the larger models), Orpheus (built on Llama 3.2, so Llama
   terms apply), KittenTTS 2 (licence ends above $1M revenue or funding) and IndexTTS-2
   (custom licence that says to contact bilibili for commercial use).

   **Owner exception (2026-11-20): Higgs TTS 2.** Its licence needs an expanded licence from
   Boson AI above 100,000 annual active users. The owner accepts that and will buy the
   expanded licence before Voxa reaches that size (see APP_ROADMAP, Blocked on owner).
2. **No espeak-ng in the App Store build.** espeak-ng is GPL-3.0. That conflicts with App Store
   terms and would make a closed app GPL. Consequences:
   - Kokoro ships for **English, Japanese and Chinese** only, since those don't need espeak.
   - Spanish, French, Italian, Portuguese and Hindi come from models that take plain text:
     Supertonic, Pocket TTS, Qwen3-TTS, Chatterbox Multilingual.
   - Piper is out: its code is now GPL-3.0, and its voices inherit their datasets' licences.
3. **Licences with conditions are passed on in Voxa's terms.**
   - Supertonic (OpenRAIL-M): its use restrictions must flow down to users.
   - Pocket TTS and Kyutai (CC-BY-4.0): attribution required.
   - Fluid Speech Pro (CC-BY-4.0): credit "Fluid Speech Pro by FluidVoice. Built on NVIDIA
     Parakeet TDT 0.6B v3 and Moondream Parakeet Ultra."
   - NVIDIA RE-USE (OpenMDW-1.1): keep the licence text and notices.
   - Higgs TTS 2 (Boson Higgs Audio 2 Community License, based on Meta's Llama 3 licence):
     - Ship both licence texts.
     - Show "Built with Higgs Materials licensed from Boson AI USA, Inc., Copyright Boson AI
       USA, Inc., All Rights Reserved and Meta Llama 3 licensed under the Meta Llama 3
       Community License, Copyright Meta Platforms, Inc., All Rights Reserved" on the
       website and the Credits screen.
     - Include the Meta Llama 3 notice file.
     - Pass Meta's Llama acceptable use policy on to users in Voxa's terms.
     - Don't use its outputs to improve any other language model.
     - A model Voxa fine-tunes from it must have a name that starts with "Higgs Audio 2".
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
| **Voxa Studio Expressive** | Higgs TTS 2 (Boson AI), 3.6B + 2.2B audio, Boson community licence (see rule 1) | en, zh, de, ko | Yes, plus multi-speaker | Most expressive voices: emotion, natural dialogue, humming |
| (parked) | FireRedTTS-2 1.5B, Apache-2.0 (see below) | en, zh, ja, ko, fr, de, ru | Research only (see below) | Multilingual dialogue: up to 4 speakers, 3 minutes |

**Locked in for the GPU tier: Higgs TTS 2** (<https://huggingface.co/bosonai/higgs-tts-2-3b-base>,
2026-11-20). It's the most expressive open model reviewed so far. Its authors report beating
gpt-4o-mini-tts on emotions (75.7%) and on questions (55.7%). It does natural multi-speaker
dialogue, voice cloning, humming, and speech with background music.

- **Model:** built on Llama-3.2-3B, with a 3.6B language model plus 2.2B for audio and 24 kHz
  output. It needs a large NVIDIA GPU, so it's GPU tier only.
- **Fit:** it runs as a worker (WORKERS.md), like Voxa Studio.
- **Cloning:** goes through the "Your voices" consent check and watermark, like every other
  cloning model.
- **Before shipping it:**
  - Build the worker and its pack.
  - Run a listening test and speed test on a GPU against Voxa Studio.
  - Put the attribution and use-policy pass-through from rule 3 in place.

**Parked: FireRedTTS-2** (<https://huggingface.co/FireRedTeam/FireRedTTS2>, parked 2026-11-19).
It is the multilingual candidate for Voxa Dialogue, next to Dia2 (English only). It
generates up to 3 minutes of dialogue between up to 4 speakers, switching voices and
adjusting tone between turns. That suits podcasts and dialogue scenes.

- **Model:** Qwen2.5-1.5B backbone. Two 8.3 GB checkpoints plus a 4.3 GB codec, 24 kHz
  output. The authors report 140 ms to the first audio on an NVIDIA L20.
- **Fit:** GPU tier only. It would run as a worker (WORKERS.md) with a script format that
  marks each speaker.

Not added, until all of these are met:

1. **Legal read on cloning.** The licence is Apache-2.0, but the README marks zero-shot
   cloning "solely for academic research purposes". Until a lawyer clears it, ship only
   its random voices, or none.
2. **Training data.** The paper describes 1.1M hours of speech and 300k hours of dialogue
   but names no sources. It needs the same provenance check that ruled out ZipVoice.
3. **Listening test on a GPU,** against Voxa Studio (Qwen3-TTS) and Dia2, in English and
   Chinese.
4. **Maintenance.** It was released in September 2025, and the October 2025 roadmap items
   (fine-tuning code, better multilingual support) were still open in November 2026.
   Check for a newer release first.

Clean-up on the GPU: **RE-USE** (OpenMDW-1.1, commercial OK) for film dialogue.

**Headless mode:** the desktop app also runs as `voxa serve` on a home server or GPU box, and
other devices on the same network can use it. It's the same licence and still nothing leaves
your network.

### Speech to text (decided 2026-11-20)

**Fluid Speech Pro** (<https://huggingface.co/FluidVoice-Labs/fluid-speech-pro>) is a speech
recognition model.
- **Licence:** CC-BY-4.0, and so are both its parents (NVIDIA Parakeet TDT 0.6B v3,
  Moondream Parakeet Ultra).
- **Accuracy:** #2 open model on the Open ASR Leaderboard (6.06% average WER), and the most
  accurate under 1B parameters.
- **Languages:** 25 European languages, detected automatically. No Arabic, Chinese or
  Japanese.
- **Format:** there's an ONNX build for sherpa-onnx, about 850 MB as int8, so it's
  computer tier.

Two uses are locked in:

1. **Test judge for European voices (roadmap M13).** The quality checks score a voice by
   transcribing it back.
   - For es, fr, de, it, pt and other European languages, Fluid Speech Pro replaces
     Whisper as the judge.
   - Whisper stays for English and for languages Fluid doesn't cover (zh, ja, ko, hi, ar).
   - This is test tooling only and doesn't ship.
2. **Voxa Captions: transcripts and subtitles (roadmap M14).**
   - Turn a recording or a film's dialogue into text with word timings.
   - Export SRT and VTT subtitles and a plain transcript.
   - Optionally use "clean mode", which drops repeated and filler words.
   - It pairs with Clean up audio: clean the dialogue first, then caption it.
   - Computer tier, signed model pack, nothing uploaded.

Later, it can also check the consent sentence for voice cloning in 25 languages, once a
multilingual cloning voice ships.

### Runtimes

- **sherpa-onnx 2.x** (Apache-2.0): Phone tier and CPU denoisers on iOS, Android, Windows,
  Linux and macOS. It supports Kokoro, Kitten, Pocket TTS, Supertonic and GTCRN, and has no
  espeak since 2.0.
- **mlx-audio** (MIT): Apple Silicon Computer and GPU tiers. It supports Kokoro, Kitten,
  Qwen3-TTS, Chatterbox, Soprano, Marvis, VoxCPM2, MOSS-Nano, DeepFilterNet and RE-USE.
- **PyTorch / CUDA worker**: GPU tier on Windows/Linux, and headless mode.

## Voice cloning policy

**Decided: cloning ships in v1.**

Cloning runs on the device, so Voxa cannot moderate it afterwards. The safeguards are built
into the app:

- **Consent recording.** Before a voice is cloned, the person reads a short consent sentence
  aloud in the app, live. The sentence includes their name and the date.
- **Same-speaker check.** An on-device speaker-verification model compares the consent
  recording with the cloning sample. The voice is created only if they match. Example model:
  3D-Speaker embeddings via sherpa-onnx, Apache-2.0; licence to be confirmed.
  - This allows the film use case (an actor records consent once, and the voice is used for
    ADR) and blocks cloning a stranger from a downloaded clip.
- **Watermark.** Prefer cloning models with a built-in watermark (Chatterbox has one). Add an
  inaudible watermark to all cloned output where the model has none.
- **Labelled voices.** Cloned voices are marked as cloned in the app and in exported file
  metadata.
- **Terms.** Impersonation, fraud and cloning without consent are forbidden. The terms
  include the use restrictions that model licences require Voxa to pass on (OpenRAIL-M,
  Pocket TTS's prohibited uses).

Cloning-capable models, by tier:

| Tier | Models |
|---|---|
| 📱 | Pocket TTS (cloning build, gated) and Chatterbox Nano. Both need testing on phones |
| 💻 | Chatterbox Nano/Flash, MOSS-TTS-Nano, Marvis, NeuTTS Air |
| 🖥️ | Qwen3-TTS, Chatterbox Multilingual, Dia2, CosyVoice3 |

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

## Purchase and licence (decided)

**Decided:**
- One payment buys the whole application: every voice, every tier, cloning and clean-up.
- No subscription.
- You install it on your machine and use it as much as you want.
- If you change machines, you deactivate the old installation and get a new activation.

### How activation works (proposed design)

- **Activate once, then fully offline.**
  - On first launch the app sends the licence key and an anonymous device ID to Voxa's
    licence server once.
  - The server returns a **signed licence file** (Ed25519) bound to that device.
  - From then on the app checks the signature locally. It never needs the internet again
    to run.
  - This keeps the "works forever, offline" promise even if Voxa's servers are down.
- **Transfer.**
  - "Deactivate this device" in the app frees the activation; the server records it.
  - If the old machine is dead or lost, the user requests a reset on Voxa's account page
    or by emailing support.
  - Self-service resets are rate-limited (for example 3 a year); support can always do more.
- **Activations per licence: two (decided).** Any two devices, for example one computer and
  one phone. Deactivating one frees its slot.
- **If Voxa ever shuts down**, publish a final update or unlock that removes the activation
  check, so buyers keep what they paid for.
- **Model downloads still need a server** the first time. Mirror the catalogue to a second
  host (for example a Voxa organisation on Hugging Face) so downloads survive an outage. The
  app also lets users export and import model packs.

### App stores are different

- On **iPhone/iPad, Mac App Store and Google Play**, the store sells the app. A purchase is
  tied to the person's Apple or Google account, and they can install it on all their own
  devices. Store rules don't allow Voxa to add its own per-device activation on top.
- So the store versions use the store's one-time purchase as-is.
- The **activation system above applies to copies sold directly** (Paddle) for Mac, Windows
  and Linux.

### Updates

- Every catalogue voice the installed app version can run is included, forever.
- **To be decided before launch:** whether major app versions that add new model
  architectures are free for existing buyers or an optional discounted upgrade.

## First steps

1. Build the device-lab benchmark for the 📱 tier (Kitten nano, Kokoro int8, Supertonic 3,
   Pocket TTS) on sherpa-onnx, on 3–5 real phones.
2. Prototype `catalogue.json` and the download, hash-check and keep-old-versions flow.
3. Run listening tests of Supertonic and Pocket TTS for es, fr, it, pt and hi against today's
   espeak-based Kokoro voices.
