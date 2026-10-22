# Voxa — server-side Kokoro TTS.
# The model is baked into the image so there is no first-request download.
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
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install "kokoro>=0.9.4" soundfile numpy

# Pre-download the model at build time so containers start without a fetch.
RUN python -c "from kokoro import KPipeline; KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M')"

COPY server.py /app/server.py

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=180s \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["python", "server.py"]
