# Model workers

Voices that need Python with PyTorch/CUDA or MLX, such as Qwen3-TTS (Voxa Studio) and
RE-USE, run in a separate **worker process**. The app starts the worker from a signed pack
and talks to it over a small protocol. The phone and computer voices run inside the app
and don't use this.

## A worker pack

A normal signed voice pack (see [CATALOGUE.md](CATALOGUE.md)) with `"engine": "worker"`.
It holds the worker's code and model files, plus `worker.json`:

```json
{
  "protocol": 1,
  "family": "Voxa Studio",
  "command": ["python3", "-I", "worker.py"],
  "voices": [{"id": "aria", "name": "Aria", "gender": "female", "language": "en-US"}],
  "start_timeout_s": 300,
  "request_timeout_s": 600
}
```

- **`command`** runs in the pack's folder. It may only be `python3`/`python` or a program
  inside the pack. The pack is signed, and this rule also stops it from naming any other
  program on the machine.
- **`voices`** are listed here, so the voice list loads without starting the worker. When
  the worker starts, it must offer every voice listed.
- **Timeouts:** `start_timeout_s` covers loading the model (the `hello` reply);
  `request_timeout_s` covers each sentence.

## The protocol (version 1)

Newline-delimited JSON over the worker's stdin and stdout, one reply per request with the
same `id`. Logs go to **stderr only**; anything else on stdout is ignored.

| Request | Reply |
|---|---|
| `{"id":1,"op":"hello","protocol":1}` | `{"id":1,"ok":true,"protocol":1,"sample_rate":24000,"voices":["aria"]}` |
| `{"id":2,"op":"synthesize","text":"…","voice":"aria","speed":1.0}` | `{"id":2,"ok":true,"sample_rate":24000,"samples":"<base64 float32 LE>"}` |
| `{"id":3,"op":"shutdown"}` | `{"id":3,"ok":true}` |
| any failure | `{"id":2,"ok":false,"error":"plain words"}` |

The app sends one sentence per `synthesize` and joins the sentences itself.

**When things go wrong:**
- **Error reply:** the user sees the error, and the worker keeps running.
- **Worker dies** (a crash, or running out of memory): it is started again once.
- **Worker stops answering:** past the timeout it is killed.

## Writing a worker

`app/workers/voxa_worker.py` holds the protocol loop. A worker is a class with
`sample_rate`, `voices` and `synthesize(text, voice, speed)`, which returns floats in
[-1, 1] (a list or a NumPy array). It passes an instance to `voxa_worker.serve`.
`app/workers/reference_worker.py` is a complete example with no dependencies (it plays
tones). The app's tests use it on every OS.
