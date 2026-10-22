# Voxa — server-side Kokoro TTS.
# The model, all 54 voices and every language's G2P data are baked into the image, so
# the running server never fetches from Hugging Face (which rate-limits anonymous pulls).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/model \
    PORT=8080

# espeak-ng is required by Kokoro's G2P (text -> phonemes) for every language.
# libsndfile1 is needed by soundfile to write WAV.
RUN apt-get update && apt-get install -y --no-install-recommends \
        espeak-ng libsndfile1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU-only torch: far smaller than the CUDA build, and this runs on a CPU machine.
# Chinese needs misaki[zh]. Japanese needs pyopenjtalk (source-only, so a compiler is
# installed and removed in the same layer) plus fugashi with the small unidic-lite
# dictionary. misaki[ja] is avoided on purpose: it pulls `unidic`, whose 770 MB dictionary
# is a separate download, and fugashi prefers it over unidic-lite and then fails to start.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake \
    && pip install --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install "kokoro>=0.9.4" soundfile numpy "misaki[zh]>=0.9.4" cryptography \
        fugashi jaconv mojimoji pyopenjtalk unidic-lite \
    && apt-get purge -y build-essential cmake && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Pre-download the weights and every voice, then build each language's pipeline once so
# anything a G2P fetches on first use (e.g. spaCy's English model) lands in the image.
RUN python -c "\
from huggingface_hub import snapshot_download; \
snapshot_download('hexgrad/Kokoro-82M', allow_patterns=['config.json', 'kokoro-v1_0.pth', 'voices/*']); \
from kokoro import KModel, KPipeline; \
m = KModel(repo_id='hexgrad/Kokoro-82M').eval(); \
[list(KPipeline(lang_code=l, repo_id='hexgrad/Kokoro-82M', model=m)('ok', voice=v)) \
 for l, v in [('a','af_heart'),('b','bf_emma'),('e','ef_dora'),('f','ff_siwis'),('h','hf_alpha'), \
              ('i','if_sara'),('j','jf_alpha'),('p','pf_dora'),('z','zf_xiaobei')]]"

# Voxa Lite's browser package, version- and hash-pinned, served by the app itself.
COPY scripts/fetch_phonon.py /tmp/fetch_phonon.py
RUN python /tmp/fetch_phonon.py /app/static && rm /tmp/fetch_phonon.py

# Everything is local now; fail fast rather than reach the network at runtime.
ENV HF_HUB_OFFLINE=1

COPY server.py voices.json /app/
COPY web /app/web

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=180s \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["python", "server.py"]
