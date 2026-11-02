# Voxa native app: build roadmap

The plan is in [NATIVE_APP_VOICES.md](NATIVE_APP_VOICES.md). This file is the work list the
build loop follows. **Each loop round takes the first unchecked milestone, finishes it,
checks it off here in the same PR, and merges it once CI is green.**

## Decisions this roadmap follows

- **Framework: Tauri 2.** The app lives in `app/`.
  - Rust core: `app/src-tauri/` with crates under `app/crates/`.
  - Web UI: reuses the look of `web/`.
  - Targets: macOS, Windows, Linux, iOS and Android.
- **The website stays as it is** (`server.py`, `web/`, billing) until the app launches. The
  loop does not touch the live site's behaviour. Website changes are a launch milestone.
- **Autonomy:** the loop merges its own PR when CI is green.
  - It never skips, disables or weakens a test to get green.
  - It stops and asks when blocked on something only the owner can do: accounts, secrets,
    money, a real device, legal text.
- **Licensing rules** from the plan are hard requirements:
  - no GPL code (espeak-ng, piper1-gpl) in the app
  - only commercial-OK model weights
  - credits recorded for every model

## Rules for every round

- Keep each milestone a reviewable PR. Split a milestone that grows too big into
  sub-milestones here first.
- Rust: `cargo fmt --check`, `cargo clippy -- -D warnings` and `cargo test` must pass. Web
  UI: its tests must pass. CI (`.github/workflows/app.yml`) runs the same.
- Model weights are never committed. Tests use tiny fixtures or are marked to need a
  download, and the network-free tests run in CI.
- Anything not verifiable here is written down as "needs a real device / owner" in the PR
  body and under **Blocked on owner** below, instead of being claimed as working. That
  includes iOS signing, phone speed and store submission.

## Milestones

- [x] **M1 Scaffold.** Tauri 2 app in `app/`: Rust workspace, minimal UI (text box, voice
  picker placeholder, play), `app.yml` CI building and testing on Linux plus a macOS and
  Windows build check. README section on running it.
- [x] **M2 Catalogue and downloads.**
  - The `catalogue.json` schema from the plan.
  - Ed25519 signature check of the catalogue.
  - Model downloader with sha256 verification, resume, and a store that keeps old versions
    (never deletes silently).
  - Unit tests with a local test server.
- **M3 First on-device voice.** Split after a prototype on 2026-11-02:
  - Kokoro int8 runs through the official `sherpa-onnx` 2.0.1 crate at RTF 0.71 on 4 CPU
    cores.
  - Lexicon mode silently **skips every word not in the dictionary**: names, plurals,
    numbers, dates.
  - The sherpa-onnx `lexicon-us-en.txt` is exactly misaki's `us_gold` + `us_silver`
    (Apache-2.0), so Voxa builds its own copy from misaki.
  - [x] **M3a Kokoro engine.** A `voxa-engines` crate wrapping sherpa-onnx (static libs)
    behind the `Engine` trait. Kokoro takes phoneme input from Voxa's G2P, never from a
    lexicon. A script builds the misaki lexicon with its licence recorded. An integration
    test that needs the model is `#[ignore]`d in CI and run locally.
  - [x] **M3b English G2P** (`voxa-g2p`, no GPL):
    - normalisation: numbers, ordinals, years, money, times, dates, common abbreviations
    - punctuation passed through
    - misaki lexicon lookup with case and possessives
    - suffix morphology (-s/-es, -ed, -ing)
    - fallback for unknown words: letter names for acronyms, letter-to-sound rules
      otherwise
    - quality gate: no word may be silently dropped
    - Result: Kokoro + this G2P, transcribed by Whisper tiny.en, scores a **1.8% word error
      rate** (af_heart) and 0.9% (am_michael) on 12 test sentences with numbers, money,
      dates and names. The remaining errors are Whisper's ("sea shore"). Measure it with
      `cargo run --release -p voxa-engines --example roundtrip`.
  - [x] **M3c Wire into the app.**
    - A development catalogue (dev signing key) listing Kokoro int8.
    - Install with progress from the UI.
    - Voices come from installed models, using their Voxa names (`voices.json`), and
      replace the tone engine.
    - Done as **signed voice packs** (import from a folder), because the mirror is blocked
      on the owner. Packs are verified like downloads. Checked in the real window: the
      20 US voices show under their Voxa names, Harper is the default, and "Read aloud"
      speaks.
    - Follow-ups:
      - the signature blends (Aurora, Atlas), which need style mixing, not a speaker id
      - British voices, using misaki's `gb_*` dictionaries
  - [x] **M3d Kitten nano.** Kitten was trained on espeak-style IPA, so it needs its own
    permissive lexicon (CMUdict → IPA, or misaki → espeak-IPA mapping). The sherpa-onnx
    release has kitten-nano v0.2; check for 0.8.
    - Done with a symbol mapping (misaki → espeak-style IPA), not a second lexicon: no
      espeak code or data. sherpa-onnx publishes kitten-nano 0.8 int8 (24 MB, 8 speakers).
    - The Whisper round trip also exposed a G2P bug: "the" and "a" always got their
      stressed forms. Fixed with misaki's context rules (ðə/ði, ɐ).
    - Word error rate, now 0.9% for both:

      | Voice | Before | After | RTF on 4 CPUs |
      |---|---|---|---|
      | Kitten nano | 4.4% | 0.9% | 0.19 |
      | Kokoro | 1.8% | 0.9% | 0.66 |

    - Kitten voices are named in the app (Luna, Nell, Tess, Zoe, Ben, Leo, Sam, Max), not
      in voices.json, because the website reads that file.
- [x] **M4 Voice library UI.** Voice cards with the device label (📱/💻/🖥️), languages,
  size, cloning flag, licence and credit; download/remove; Credits screen. Voices carry
  their Voxa names from `voices.json` where they exist.
  - Model cards show the family name (Voxa Classic = Kokoro, Voxa Instant = Kitten), the
    device label, voice count, languages, size, licence and credit, and whether the model
    clones voices, with Remove. The voice picker groups by family, accent and gender, and
    has a **Preview** button.
  - **Downloads:** "Available to download" lists the signed catalogue's models for this
    app version, with Install and live progress. It is hidden until a build sets
    `VOXA_CATALOGUE_URL`, which needs the mirror (blocked on owner). Tested end to end
    against a local server: progress, a min_app filter, and refusal of forged or older
    catalogues.
  - **Credits & licences:** the installed models, the native engines, and all 489 Rust
    libraries from `cargo metadata`.
  - `tools/licences.py` is now a **CI licence gate**: the build fails if any dependency's
    licence cannot be met with permissive licences (no GPL, AGPL or LGPL), or if the
    credits list is stale.
- [x] **M5 Device benchmark.** An in-app benchmark: speed (real-time factor), first-audio
  latency and peak memory per voice, exportable as JSON. These numbers feed the voice-card
  labels.
  - The **Speed test** section runs every installed model on a fixed paragraph and
    reports:
    - cold load time
    - time to first audio (the first sentence)
    - speed (1/RTF)
    - peak and model memory
    - device details (OS, CPU, cores, RAM)

    Results save as JSON.
  - Measured in the app on the 4-core cloud dev box (Intel Xeon, 16 GB), with the webview
    running:

    | Model | Load | First audio | Speed | Memory |
    |---|---|---|---|---|
    | Voxa Instant (Kitten nano) | 3.0 s | 0.6 s | 5.5× real time | 206 MB |
    | Voxa Classic (Kokoro int8) | 7.5 s | 1.9 s | 1.4× real time | 402 MB |

  - **Found and fixed:** Kitten's voices speak at very different rates for the same
    setting. On the benchmark paragraph they took 21–32 s, against Kokoro's 18 s. Each
    Kitten voice now has a measured pace factor (1.15–1.73) applied after the user's
    speed. With it, all eight read the paragraph within 15% of each other, and the
    Whisper round trip at those paces scores 0–2.6% WER.
  - The phone, laptop and GPU numbers for the voice cards need runs on real devices
    (blocked on owner).

- [x] **M6 Licence and activation.**
  - Licence server endpoints: activate, deactivate, reset; two activations per licence; a
    signed Ed25519 licence file bound to the device.
  - Offline verification in the app.
  - The server runs separately from the live site, or behind a flag that is off by default.
  - Tests cover the activation limit, transfer and tampered files.
  - Done:
    - `licence-server/`: Python stdlib plus `cryptography`, SQLite, keys stored as hashes
      only. Two seats, free re-activation on the same device, deactivate, and reset limited
      to 3 a year (admin reset unlimited).
    - `app/crates/voxa-licence`: offline verification, an anonymous device id, the client.
    - In the app, enabled only when the build sets `VOXA_LICENCE_URL` and
      `VOXA_LICENCE_PUBKEY`. Development and store builds never ask.
  - **Verified:**
    - A Python-signed test vector is checked by both sides.
    - An end-to-end test of the Rust client against the real server runs in CI: two seats,
      the third refused, deactivate, reset, and offline use afterwards.
    - In the real window: activate with a key, and reading aloud works once activated.
- [x] **M7 Multilingual phone voices.** Supertonic 3 and Pocket TTS (no-cloning build)
  through sherpa-onnx for es, fr, it, pt, de and hi. Pass on OpenRAIL-M and CC-BY terms in
  the app's terms and credits.
  - Done with **Supertonic 3** (`sherpa-onnx-supertonic-3-tts-int8-2026-05-11`, 145 MB),
    shown as **Voxa World**.
    - Plain text in, no G2P.
    - 10 speakers, 44.1 kHz, about 7× real time on 4 CPU cores.
    - Voices: Ana, Elena, Mira, Nina, Sofia, Adrian, Daniel, Teo, Luca, Hugo, in each of
      es, fr, de, it, pt and hi.
  - Speech checked by transcribing it with Whisper base:
    - German: a perfect transcript.
    - Spanish and Italian: one sound-alike word each.
    - French: one sound-alike word.
    - Portuguese: the weakest; two words misheard.
    - Hindi: correct words, in romanised script.

    Supertonic offers 31 languages; others can be added after the same check.
  - **Licence:** the model is under BigScience Open RAIL-M. The package ships the model and
    code licences, and Attachment A (use restrictions) appears in Credits under the model.
    That is the pass-on the licence requires.
  - The app gained a **Language** picker that filters voices, and the Preview line now
    speaks the voice's own language.
  - **Pocket TTS moved to M9.** sherpa-onnx's build of it is the voice-cloning variant (it
    needs reference audio) and English-only, so it belongs with cloning.
- [x] **M8 Audio clean-up.**
  - GTCRN and DeepFilterNet 3 denoise.
  - A strength (wet/dry) mix and an A/B player.
  - Output the same length as the input, kept at 48 kHz/24-bit, with tests for both.
  - Models, both through sherpa-onnx:
    - **Voxa Clean Studio:** DPDFNet 48 kHz HR (Ceva, Apache-2.0), full band.
    - **Voxa Clean Fast:** GTCRN (MIT), which works at 16 kHz.
    - DeepFilterNet 3 isn't in sherpa-onnx; DPDFNet is the full-band equivalent there.
  - Measured with `examples/clean_check.rs` (speech plus white noise and mains hum):

    | Model | Input | SNR before → after | Delay | Speed |
    |---|---|---|---|---|
    | Studio | 48 kHz | 0 → 17.7 dB | 0 samples | 5.3× real time |
    | Studio | 48 kHz | 10 → 23.4 dB | 0 samples | 5.1× real time |
    | Studio | 44.1 kHz | 0 → 17.3 dB | 0 samples | 5.3× real time |
    | Fast | 48 kHz | 0 → 15.7 dB | 0 samples | 24× real time |

    Length is always exact. The models drop a partial last frame, so the input is padded
    and the output trimmed.
  - WAV in: 16/24/32-bit integer and float, any channel count, WAVE_FORMAT_EXTENSIBLE.
    WAV out: 24-bit, with the same rate, channels and frames, and every extra chunk (BWF
    `bext` timecode, `iXML`, `LIST`) kept in order.
  - Strength (wet/dry) is applied after cleaning, so the slider never re-runs the model. A
    strength of 0 gives back the original.
  - **Verified in the real window**, using the real open and save dialogs: a noisy stereo
    48 kHz take with a `bext` chunk was opened, cleaned, auditioned A/B and saved. Checked
    independently afterwards:
    - same 24-bit stereo 48 kHz format
    - 629,754 frames, identical to the input
    - `bext` identical
    - SNR from -2.3 to 16.0 dB
  - **RE-USE** (NVIDIA, for badly damaged film audio) needs CUDA or Metal and is planned
    for the GPU tier (M11).

- **M9 Voice cloning with consent.**
  - A consent-recording flow and an on-device same-speaker check (speaker embeddings via
    sherpa-onnx).
  - Cloned voices labelled in the app and in file metadata, and a watermark.
  - First cloning model: the Chatterbox family or Pocket TTS, whichever runs through the
    stack.
  - Prototype findings (2026-11-02):
    - **Speaker check (CAM++, Apache-2.0).** Halves of the same real recording score
      0.92–0.97 cosine similarity; different real speakers score 0.13–0.62. A 0.75
      threshold separates them.
    - **Synthetic voices fool the speaker check:** Supertonic voices score 0.82–0.91
      against each other. So consent must be a **live recording with random code words**
      that speech recognition checks; a pre-made clip then fails.
    - **Pocket TTS** (CC-BY-4.0, English, sherpa int8 build): clones run about 2× real
      time, but similarity to the reference is weak (0.27–0.37). Cloning ships as
      **Preview**, after a listening check.
    - **ZipVoice was rejected:** the only sherpa build is trained on Emilia, which is
      CC-BY-NC.
  - [x] **M9a Consent engine.**
    - A challenge sentence with the person's name, today's date and three random code
      words.
    - Whisper tiny.en (MIT) transcribes the recording; the name and every code word must
      be heard.
    - The CAM++ speaker embedding must match the cloning sample at 0.75 or more.
    - Minimum lengths: 4 s for consent, 10 s for the sample.
    - The result is a consent record holding hashes of both recordings.
    - Done as `voxa-engines::consent`. Tested on real speech:
      - the same speaker passes, with similarity above 0.9
      - a different speaker fails
      - a recording without the right code words fails
    - Small recognition slips are forgiven ("harbour" for "harbor"); different words are
      not ("tigress" for "tiger").
  - [ ] **M9b Cloned voices.**
    - A Pocket TTS engine that takes reference audio.
    - "Your voices" are stored with their consent records, shown as cloned, and
      removable.
  - [ ] **M9c Recording UI, watermark and labels.**
    - Live microphone recording only; no uploaded files for consent.
    - An inaudible keyed watermark on cloned output, with a detector.
    - Exported files are labelled as a cloned voice (WAV INFO chunk).

- [ ] **M10 Mobile builds.** Android build in CI, iOS build in CI on a macOS runner
  (unsigned). Mobile layout. Signing and stores go under **Blocked on owner**.
- [ ] **M11 Computer and GPU tiers, and headless mode.**
  - A model-worker interface for larger models (MLX on Apple Silicon; a PyTorch/CUDA
    worker elsewhere).
  - First model: Qwen3-TTS.
  - `voxa serve` headless mode on the local network.
- [ ] **M12 Launch changes to the website.** Only when the owner says the app is launching:
  - remove the paid tier, billing and the GPU path from the site
  - make the free site a demo with a "Get the app" button
  - the subscriber migration plan

## Blocked on owner

(The loop adds items here: accounts, signing certificates, real-device test results,
store listings, legal review.)

- **Catalogue signing key** (from M2). Run `voxa-catalogue keygen` on your own machine, keep
  the secret key offline, and send the public key so it can be built into the app. See
  [CATALOGUE.md](CATALOGUE.md). Until then the app is built with a development key.
- **Licence server hosting and key** (from M6):
  - Run `licence-server/` on a small host with HTTPS (for example the Fly or Railway setup
    the site already uses).
  - Generate its signing key with `licence_server.py keygen` and keep the secret safe.
  - Send the public key, which is built into release builds as `VOXA_LICENCE_PUBKEY`.
- **Selling licences** (from M6, for launch):
  - Connect the Paddle checkout to `licence_server.py create`, so each purchase emails a
    licence key.
  - Write the licence and refund terms, including the 2-device rule and resets. This
    needs legal review.
- **Listening checks** (from M7):
  - Supertonic's export has no speaker names. By pitch, speakers 0–3 are clearly female
    and 6–9 clearly male. Speakers 4 ("Sofia", labelled female) and 5 ("Adrian", labelled
    male) are both about 170 Hz, so their labels follow Supertonic's F1–F5 / M1–M5 order.
    Listen to both and say if either is wrong.
  - Also, a native-speaker listen to the Portuguese voices, the weakest in the
    transcription check.
- **Speed tests on real devices** (from M5). Install the app on 3–5 devices: an old
  phone and a new one once M10 makes mobile builds, an Apple Silicon Mac, a Windows
  laptop, and an NVIDIA machine. Run **Speed test** on each and send the saved JSON
  files. They become the measured requirements in the catalogue
  (`requirements.measured: true`) and the numbers on the voice cards.
- **Model mirror hosting** (from M2). Somewhere to host model files over https, for example
  Cloudflare R2, a Hugging Face organisation, or both as primary and backup. Needs an account
  in Voxa's name.
