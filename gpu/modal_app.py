"""Voxa's GPU service on Modal: Kokoro on a GPU for paid users' audiobooks.

    modal secret create voxa-gpu VOXA_GPU_TOKEN=<long random string>
    modal deploy gpu/modal_app.py          # prints the service URL
    modal run gpu/modal_app.py::benchmark  # speed and cost on T4 and L4

It serves the same routes as `python gpu/service.py` (POST /synthesize, GET /health) with the
same code, so the Voxa server cannot tell them apart. Set VOXA_GPU_URL to the printed URL
and VOXA_GPU_TOKEN to the same token on Fly; see docs/GPU.md.

Containers start on demand and stop after SCALEDOWN seconds idle, so an idle service costs
nothing. MAX_CONTAINERS caps how many GPUs can run at once, which caps the bill.
"""
import os
import time

import modal

GPU = 'L4'
SCALEDOWN = 300
MAX_CONTAINERS = 4
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
VOICES = ['af_heart', 'bf_emma', 'ef_dora', 'ff_siwis', 'hf_alpha', 'if_sara', 'jf_alpha', 'pf_dora', 'zf_xiaobei']


def bake_model():
    """Download the weights and every voice, and build each language's pipeline once, so
    a container never fetches from Hugging Face (which rate-limits) at request time."""
    from huggingface_hub import snapshot_download
    snapshot_download('hexgrad/Kokoro-82M', allow_patterns=['config.json', 'kokoro-v1_0.pth', 'voices/*'])
    from kokoro import KModel, KPipeline
    model = KModel(repo_id='hexgrad/Kokoro-82M').eval()
    for voice in VOICES:
        list(KPipeline(lang_code=voice[0], repo_id='hexgrad/Kokoro-82M', model=model)('ok', voice=voice))


image = (
    modal.Image.debian_slim(python_version='3.11')
    .apt_install('espeak-ng', 'libsndfile1', 'build-essential', 'cmake')
    # The same packages as the Dockerfile; torch from PyPI includes CUDA.
    .pip_install('torch', 'kokoro>=0.9.4', 'soundfile', 'numpy', 'misaki[zh]>=0.9.4',
                 'fugashi', 'jaconv', 'mojimoji', 'pyopenjtalk', 'unidic-lite', 'cryptography',
                 'fastapi[standard]')
    .env({'HF_HOME': '/model'})
    .run_function(bake_model)
    .env({'HF_HUB_OFFLINE': '1', 'VOXA_DEVICE': 'cuda'})
    .add_local_file(os.path.join(ROOT, 'server.py'), '/root/voxa/server.py')
    .add_local_file(os.path.join(ROOT, 'voices.json'), '/root/voxa/voices.json')
    .add_local_file(os.path.join(HERE, 'service.py'), '/root/voxa/gpu/service.py')
)

def build_api(service):
    """The FastAPI app Modal serves: the same routes and answers as gpu/service.py."""
    from fastapi import FastAPI, Request, Response
    from fastapi.responses import JSONResponse

    server = service.server
    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @api.get('/health')
    def health():
        return {'ok': True, 'device': server.DEVICE}

    @api.post('/synthesize')
    async def synthesize(request: Request):
        try:
            service.check_token(request.headers.get('authorization'), os.environ.get('VOXA_GPU_TOKEN', ''))
        except service.Unauthorized:
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        try:
            body = await request.json()
            return Response(service.synthesize(body), media_type='audio/wav')
        except (server.BadRequest, ValueError) as e:
            return JSONResponse({'error': str(e)}, status_code=400)

    return api


app = modal.App('voxa-gpu')


@app.cls(gpu=GPU, image=image, secrets=[modal.Secret.from_name('voxa-gpu')],
         scaledown_window=SCALEDOWN, max_containers=MAX_CONTAINERS, timeout=900)
class Voxa:
    @modal.enter()
    def load(self):
        import sys
        sys.path[:0] = ['/root/voxa', '/root/voxa/gpu']
        import service
        self.service = service
        self.started = time.time()
        service.server.get_pipeline('a')  # model onto the GPU before the first request

    @modal.asgi_app()
    def web(self):
        return build_api(self.service)

    @modal.method()
    def timed(self, text, voice='harper'):
        """For the benchmark: seconds of audio, seconds of work, and whether the model was cold."""
        cold = time.time() - self.started < 5
        t0 = time.time()
        audio = self.service.synthesize({'text': text, 'voice': voice})
        took = time.time() - t0
        return {'audio_seconds': (len(audio) - 44) / 48000, 'seconds': took, 'cold': cold}


# Modal's listed GPU prices in USD per hour, 22 October 2026 (modal.com/pricing).
PRICE_PER_HOUR = {'T4': 0.59, 'L4': 0.80}


@app.local_entrypoint()
def benchmark(minutes: float = 5.0):
    """Speed and cost per audiobook-hour on T4 and L4, from about `minutes` of audio."""
    sentence = ('The ferry left the harbour at a quarter past seven, and by the time the town '
                'had shrunk to a row of lights, most passengers had gone below. ')
    text = sentence * max(1, int(minutes * 60 * 15 / len(sentence)))  # ~15 characters a second
    chunks = [text[i:i + 1500] for i in range(0, len(text), 1500)]
    for gpu in PRICE_PER_HOUR:
        voxa = Voxa.with_options(gpu=gpu)()
        voxa.timed.remote('Warm up.')
        audio = work = 0.0
        for chunk in chunks:
            r = voxa.timed.remote(chunk)
            audio += r['audio_seconds']
            work += r['seconds']
        speed = audio / work
        per_book_hour = PRICE_PER_HOUR[gpu] / speed
        print(f'{gpu}: {speed:.0f}x real time over {audio / 60:.1f} min of audio; '
              f'a 10-hour book takes {600 / speed:.0f} min and about ${10 * per_book_hour:.2f}')
