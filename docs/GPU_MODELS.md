# GPU model list

What to host and test when Voxa gets a GPU (Modal; needs `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`
as GitHub secrets). Nothing here is built yet. "Measured" means I ran it myself; everything else is
from the model's own card, so treat speeds and scores as the authors' claims until tested.

The test for the speech models is `gpu/compare.py` (listening page plus a Whisper read-back score).
Add the other services to a benchmark of their own.

## Studio HD voices (text to speech)

| Model | Languages | Licence | Notes |
|---|---|---|---|
| **VoxCPM2** | many (default pick) | Apache-2.0 | Planned HD default. |
| **Ming-3.0-tts-0.5B** (inclusionAI) | zh, en, de, es, fr, ko, ja; Sichuanese, Cantonese dialects | MIT | Logged 2026-11-19. 1.48B parameters in total (Qwen2.5-0.5B backbone, diffusion head, audio tokenizer, speaker encoder); 2.96 GB weights; 44.1 kHz. Zero-shot voice cloning, voice design from a description, emotion/speed/pitch/volume control, 14 inline sound tags (laugh, sigh, breath; zh and en). Authors tested on H800 and H20 GPUs only; no CPU speed given. Their results: Seed-TTS WER en 1.58%, zh 0.80%; MiniMax set de 0.37%, es 1.10%, fr 4.26%, ko 1.73%, ja 3.77%. Needs the code from github.com/inclusionAI/Ming-3.0-tts (the HF repo has no inference code). No Arabic, Hindi, Italian, Portuguese or Russian. Training data not fully disclosed: ask the authors about commercial use. Cloning needs the consent rules. The "tiny" sibling is a ~18 GB mixture-of-experts: big GPUs only. A community browser build exists (850 MB, `fusorlabs/ming-3.0-tts-0.5b-web`): too large for on-device. |
| Audio8-TTS-Realistic | | Apache/MIT label | Data not disclosed: verify with authors. |
| MOSS-TTS-v1.5 | | Apache/MIT label | Data not disclosed: verify with authors. |
| NVIDIA Magpie | | NVIDIA Open Model License ("ready for commercial use") | Read the licence terms before launch. |
| Chatterbox | | MIT | |
| Egyptian-Arabic Chatterbox | ar-eg | | Ask the author what it was trained on. |
| Qwen3-TTS | | | |
| Dia2 | | | Two-speaker dialogue. |
| CosyVoice 3 | | | |
| Kokoro-82M | 9 | Apache-2.0 | Standard voices; moves from the Fly CPU machine to Modal. |
| LFM3-Omni-2B | 19 locales | free under $10M revenue | Measured on CPU: ~3x slower than real time; not a free-tier candidate. A "talk to your document" idea. |

Arabic and Hindi still have no strong pick. Ruled out as non-commercial: OmniVoice / VoiceStudio,
Higgs TTS 3, Voxtral TTS, Steer-TTS-edge, Sada (XTTS), Fish S2 Pro (research licence), and
Qwen-Audio-3.1 (API only).

## Other services

| Service | Model | Licence | Notes |
|---|---|---|---|
| **Voxa Denoise** (noise removal for audio files) | Inworld Voice Isolator v1 (`inworld/voice-isolator`) | Apache-2.0 | Logged 2026-11-19. Audio in, cleaned voice out at 48 kHz, plus the removed background. Offline model 64M parameters (256 MB; 130 MB fp16 for browsers), realtime model 58M (233 MB, ~63 ms delay). Tested by the authors on A100/H100/B200/B300 and CPU; no CPU speed given. Removes noise, music and other voices (keeps the loudest speaker); does not restore damaged audio (their v2). An in-browser WebGPU demo exists (`mrfakename/voice-isolator-webgpu`), so no plain CPU/WASM option. Plan: Studio only, charged in minutes from the same wallet, files deleted after 24 hours, an agent tool `denoise_audio`. Measure speed and quality before setting a price. |
| **Transcription** and read-back scoring | Rumble-2-Flash (`Reson8/Rumble-2-Flash`) | Apache-2.0 | Logged 2026-11-19. Speech to text, 45 languages (Arabic, Chinese, Hindi, Japanese included), auto language detection. 508.6M parameters, 1.02 GB weights (+357 MB optional CTC head); built on Qwen3-ASR. Authors' leaderboard: English WER 6.60 avg; de 5.5, es 3.7, fr 7.2, zh 6.6 (CER), hi 14.1. Measured on an H200 with vLLM only. Uses: a fairer scorer than Whisper small for the listening comparison (Arabic, Japanese, Hindi), a transcription service beside Denoise, and the first step of the parked dubbing idea. |
