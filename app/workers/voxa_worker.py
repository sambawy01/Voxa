"""Voxa's model-worker protocol, Python side (roadmap M11b).

Larger models (Qwen3-TTS on PyTorch/CUDA or MLX, RE-USE) run in a worker process that
the app starts from a signed pack. The app and the worker talk in newline-delimited JSON
over the worker's stdin and stdout; logs go to stderr.

Requests (from the app), one JSON object per line:
    {"id": 1, "op": "hello", "protocol": 1}
    {"id": 2, "op": "synthesize", "text": "...", "voice": "aria", "speed": 1.0}
    {"id": 3, "op": "clean", "sample_rate": 48000, "samples": "<base64 float32 LE>"}
    {"id": 4, "op": "shutdown"}

Replies, one per request, with the same id:
    {"id": 1, "ok": true, "protocol": 1, "sample_rate": 24000, "voices": ["aria", ...],
     "capabilities": ["synthesize"]}
    {"id": 2, "ok": true, "sample_rate": 24000, "samples": "<base64 float32 little-endian>"}
    {"id": 3, "ok": true}
    {"id": 2, "ok": false, "error": "plain words"}

A speech worker implements a class with `sample_rate`, `voices` and
`synthesize(text, voice, speed) -> list/array of floats in [-1, 1]`. A clean-up worker
(noise removal) implements `clean(samples, sample_rate) -> samples` instead, which must
return exactly as many samples as it got (film sync). Either calls `serve(engine)`.
Nothing else may be written to stdout.
"""
import base64
import json
import struct
import sys

PROTOCOL = 1


def encode_samples(samples) -> str:
    """float32 little-endian, base64. Accepts a list or anything with .tobytes() (numpy)."""
    if hasattr(samples, 'astype') and hasattr(samples, 'tobytes'):
        return base64.b64encode(samples.astype('<f4').tobytes()).decode('ascii')
    return base64.b64encode(struct.pack(f'<{len(samples)}f', *samples)).decode('ascii')


def decode_samples(b64: str):
    """The inverse of encode_samples: a NumPy array when NumPy is there, else a list."""
    raw = base64.b64decode(b64)
    if len(raw) % 4:
        raise ValueError('samples are not float32')
    try:
        import numpy
        return numpy.frombuffer(raw, dtype='<f4').copy()
    except ImportError:
        return list(struct.unpack(f'<{len(raw) // 4}f', raw))


def capabilities(engine) -> list:
    return [c for c in ('synthesize', 'clean') if callable(getattr(engine, c, None))]


def handle(engine, req: dict) -> dict:
    rid = req.get('id')
    op = req.get('op')
    try:
        if op == 'hello':
            if req.get('protocol') != PROTOCOL:
                return {'id': rid, 'ok': False,
                        'error': f'this worker speaks protocol {PROTOCOL}, the app asked for {req.get("protocol")}'}
            return {'id': rid, 'ok': True, 'protocol': PROTOCOL,
                    'sample_rate': int(getattr(engine, 'sample_rate', 0)),
                    'voices': list(getattr(engine, 'voices', [])),
                    'capabilities': capabilities(engine)}
        if op == 'synthesize':
            text, voice = req.get('text'), req.get('voice')
            speed = float(req.get('speed', 1.0))
            if not isinstance(text, str) or not text.strip():
                return {'id': rid, 'ok': False, 'error': 'no text'}
            if 'synthesize' not in capabilities(engine):
                return {'id': rid, 'ok': False, 'error': 'this worker does not speak'}
            if voice not in engine.voices:
                return {'id': rid, 'ok': False, 'error': f'unknown voice {voice!r}'}
            samples = engine.synthesize(text, voice, speed)
            return {'id': rid, 'ok': True, 'sample_rate': int(engine.sample_rate),
                    'samples': encode_samples(samples)}
        if op == 'clean':
            if 'clean' not in capabilities(engine):
                return {'id': rid, 'ok': False, 'error': 'this worker does not clean audio'}
            rate = int(req.get('sample_rate', 0))
            if rate <= 0:
                return {'id': rid, 'ok': False, 'error': 'no sample rate'}
            x = decode_samples(req.get('samples', ''))
            y = engine.clean(x, rate)
            if len(y) != len(x):
                return {'id': rid, 'ok': False,
                        'error': f'clean-up changed the length ({len(x)} -> {len(y)} samples)'}
            return {'id': rid, 'ok': True, 'sample_rate': rate, 'samples': encode_samples(y)}
        if op == 'shutdown':
            return {'id': rid, 'ok': True}
        return {'id': rid, 'ok': False, 'error': f'unknown op {op!r}'}
    except Exception as e:  # report, keep serving
        print(f'voxa worker: {op} failed: {e!r}', file=sys.stderr, flush=True)
        return {'id': rid, 'ok': False, 'error': f'{type(e).__name__}: {e}'}


def serve(engine, stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            reply = {'id': None, 'ok': False, 'error': f'bad JSON: {e}'}
            req = {}
        else:
            reply = handle(engine, req)
        stdout.write(json.dumps(reply) + '\n')
        stdout.flush()
        if req.get('op') == 'shutdown':
            return
