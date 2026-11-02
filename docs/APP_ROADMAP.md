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
- [ ] **M3 First on-device voice.**
  - sherpa-onnx (Apache-2.0) linked from Rust.
  - Kitten TTS nano and Kokoro English synthesis.
  - An espeak-free English G2P (lexicon plus rules; no GPL). Record its licence.
  - Text in, WAV out, from the UI.
- [ ] **M4 Voice library UI.** Voice cards with the device label (📱/💻/🖥️), languages,
  size, cloning flag, licence and credit; download/remove; Credits screen. Voices carry
  their Voxa names from `voices.json` where they exist.
- [ ] **M5 Device benchmark.** An in-app benchmark: speed (real-time factor), first-audio
  latency and peak memory per voice, exportable as JSON. These numbers feed the voice-card
  labels.
- [ ] **M6 Licence and activation.**
  - Licence server endpoints: activate, deactivate, reset; two activations per licence; a
    signed Ed25519 licence file bound to the device.
  - Offline verification in the app.
  - The server runs separately from the live site, or behind a flag that is off by default.
  - Tests cover the activation limit, transfer and tampered files.
- [ ] **M7 Multilingual phone voices.** Supertonic 3 and Pocket TTS (no-cloning build)
  through sherpa-onnx for es, fr, it, pt, de and hi. Pass on OpenRAIL-M and CC-BY terms in
  the app's terms and credits.
- [ ] **M8 Audio clean-up.**
  - GTCRN and DeepFilterNet 3 denoise.
  - A strength (wet/dry) mix and an A/B player.
  - Output the same length as the input, kept at 48 kHz/24-bit, with tests for both.
- [ ] **M9 Voice cloning with consent.**
  - A consent-recording flow and an on-device same-speaker check (speaker embeddings via
    sherpa-onnx).
  - Cloned voices labelled in the app and in file metadata, and a watermark.
  - First cloning model: the Chatterbox family or Pocket TTS, whichever runs through the
    stack.
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
- **Model mirror hosting** (from M2). Somewhere to host model files over https, for example
  Cloudflare R2, a Hugging Face organisation, or both as primary and backup. Needs an account
  in Voxa's name.
