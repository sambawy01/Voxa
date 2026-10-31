# Dialogue cleanup for film (planned, waiting on the GPU service)

**Status: parked.** Nothing is built. Start once the Modal GPU service in [GPU.md](GPU.md)
is deployed and `VOXA_GPU_URL` / `VOXA_GPU_TOKEN` are set on Fly.

## The idea

A filmmaker whose location audio came out noisy, echoey or thin uploads the dialogue track
(or the video) and gets back a cleaned track that drops straight into their editor in sync.

Model: [nvidia/NVIDIA-NemotronLabs-RE-USE](https://huggingface.co/nvidia/NVIDIA-NemotronLabs-RE-USE)

- 9.6M parameters, conv encoder/decoder with bidirectional Mamba.
- Fixes noise, reverb, clipping, codec artefacts, packet loss and band-limited audio;
  optional bandwidth extension.
- Mono WAV in and out, 8–48 kHz. Multilingual.
- Licence: OpenMDW v1.1, card says "ready for commercial use". **Have the licence read
  before selling it.**
- Documented hardware: NVIDIA Ampere on Linux. Mamba's kernels are CUDA-only, so this runs
  on the Modal GPU, not on Fly's CPUs. No speed numbers are published.

## Known risks (test these first)

1. **Multiple speakers.** The model keeps the main speaker and suppresses others. On a
   single boom track with two actors it may duck the second actor. If it does, the product
   is "one track per actor (lavs)" and the page must say so.
2. **Sync.** Output must be exactly the input length, sample for sample, including after
   chunking, or lips drift.
3. **Long files.** Trained on clips under 10 s; the card uses chunked inference above ~30 s.
   Check chunk boundaries for clicks or level jumps on a 10-minute file.
4. **Laughter and non-speech vocal sounds** are weaker (NVIDIA has a separate variant).
5. **Over-processing.** Fully cleaned dialogue can sound dry on screen, hence the strength
   control below.

## What filmmakers need

- **Exact sync**: output length equals input length.
- **Strength control**: wet/dry mix of enhanced and original (0–100 %), applied after
  enhancement so changing it costs no GPU time.
- **Formats**: upload WAV/BWF, or a video (MP4/MOV) and Voxa extracts the audio. Return a
  48 kHz, 24-bit WAV, keeping BWF timecode (`bext` / iXML) so it snaps to the timeline.
  Optionally return the video with the cleaned audio swapped in.
- **Stereo/multichannel**: the model is mono, so process each channel separately and
  reassemble.
- **Batches**: many takes at once, with progress through the existing job queue.
- **A/B player**: before/after toggle in the page.

## How it fits into Voxa

| Piece | Where |
|---|---|
| Enhancement on GPU | `POST /enhance` in `gpu/service.py` / `gpu/modal_app.py`, next to `/synthesize`, same bearer token |
| Uploads, jobs, quota | `server.py`: new upload endpoint, reuse the job queue and the paid-account GPU routing; quota in minutes of audio, like `VOXA_PODCAST_MINUTES_*` |
| Audio from video and back | ffmpeg; prefer extracting in the browser so gigabytes of video are not uploaded |
| Page | `web/clean.html`: upload, strength slider, A/B player, download |
| Plan | Studio (paid) only, priced per minute |
| Legal | update `web/privacy.html`: users' recordings are processed and then deleted |

## Step 1: trial (do this first)

Add `/enhance` to the Modal service and a `modal run gpu/modal_app.py::enhance_benchmark`
that, on an L4, reports:

- output length == input length, for short and chunked files
- two speakers on one track: both still audible?
- chunk seams on a 10-minute file
- speed (real-time factor) and cost per hour of audio

Use 5–10 real clips from actual footage (boom in a noisy street, echoey room, two actors on
one mic, one lav per actor), not only synthetic noise. Decide pricing and the multi-speaker
wording from the results.

## Step 2: product

Upload endpoint and minutes quota, strength mix, BWF timecode passthrough, video
extract/remux, `web/clean.html`, privacy page update.
