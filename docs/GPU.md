# GPU voices for paid accounts (Modal)

Paid accounts' Studio jobs, mostly audiobooks, run on a GPU instead of the Fly machine's
shared CPUs. Everything is built; it switches on when the Voxa server knows where the GPU
service is. Until then paid jobs run on the CPU like everyone else's.

## How it fits together

- `gpu/service.py`: the service. POST /synthesize takes the same JSON as `/api/tts` and
  returns a WAV, using Voxa's own validation, voices and Kokoro code, so the audio is the
  same as the CPU server's. It requires `Authorization: Bearer <VOXA_GPU_TOKEN>`.
- `gpu/modal_app.py`: runs that service on Modal (L4 GPU). Containers start on demand,
  stop after 5 idle minutes, and at most 4 run at once, which caps the bill.
- `server.py`: when `VOXA_GPU_URL` and `VOXA_GPU_TOKEN` are set, jobs from paid accounts go
  to the GPU on their own workers (`VOXA_GPU_WORKERS`, default 4), so they never wait behind
  free users' CPU jobs and free users never wait behind them. A GPU failure is a normal job
  error: allowances are refunded and the audiobook page retries the part.

## 1. Create the account and deploy

```bash
pip install modal
modal token new                                   # opens a browser to sign in
modal secret create voxa-gpu VOXA_GPU_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
modal deploy gpu/modal_app.py                     # the first build takes several minutes
```

`modal deploy` prints the web URL, like `https://<workspace>--voxa-gpu-voxa-web.modal.run`.
In the Modal dashboard, **Settings → Usage and billing**, set a monthly spending limit.

### From an iPad (no terminal): GitHub Actions

In the repository on github.com, **Settings → Secrets and variables → Actions → New
repository secret**, add three secrets:

- `MODAL_TOKEN_ID`: the `ak-...` value from `modal token set --token-id ak-... --token-secret as-...`
- `MODAL_TOKEN_SECRET`: the `as-...` value
- `VOXA_GPU_TOKEN`: any long random string (a password manager can generate one); Fly needs the same value

Then **Actions → GPU service (Modal) → Run workflow** (or re-run its latest run). It stores the
token in Modal, deploys, checks `/health`, runs the benchmark, and puts the service URL and
the results on the run's summary page.

## 2. Measure it

```bash
modal run gpu/modal_app.py::benchmark
```

It prints the speed and the cost of a 10-hour book on T4 and on L4, from about five minutes
of audio, for a few cents of credit. Keep `GPU = 'L4'` in `gpu/modal_app.py` unless T4 turns
out cheaper per book.

## 3. Point Voxa at it

Use the same token as the Modal secret (`modal secret list` does not show values; keep it
from step 1, or create the secret again with a new one).

```bash
fly secrets set -a voxa-zpukmw VOXA_GPU_URL=https://<workspace>--voxa-gpu-voxa-web.modal.run \
                               VOXA_GPU_TOKEN=<the token>
```

`fly logs` then shows `paid jobs go to the GPU service at ...` at startup. A paid account
(see docs/SUPABASE.md, "Making an account paid") making an audiobook is now served by the GPU.

## Running the service locally

```bash
VOXA_GPU_TOKEN=dev PORT=8090 python gpu/service.py
VOXA_GPU_URL=http://127.0.0.1:8090 VOXA_GPU_TOKEN=dev python server.py
```

On a machine without a GPU it runs on the CPU, which is how the routing is tested.
