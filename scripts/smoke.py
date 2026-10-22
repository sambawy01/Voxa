"""End-to-end check of a running Voxa: every language produces audio, bad input is a 400.

    python scripts/smoke.py                         # the live app
    python scripts/smoke.py http://localhost:8080   # a local server or container

Uses the job API the page uses, so slow generations are polled instead of timing out.
Exits non-zero if anything fails.
"""
import io
import json
import sys
import time
import urllib.error
import urllib.request
import wave

BASE = (sys.argv[1] if len(sys.argv) > 1 else 'https://voxa-zpukmw.fly.dev').rstrip('/')

SAMPLES = {
    'a': 'Hello from Voxa. This is a second sentence.',
    'b': 'Good morning, how are you?',
    'e': 'Hola, esto es una prueba.',
    'f': 'Bonjour, ceci est un test.',
    'h': 'नमस्ते, यह एक परीक्षण है।',
    'i': 'Ciao, questa è una prova.',
    'j': 'こんにちは。これはテストです。',
    'p': 'Olá, isto é um teste.',
    'z': '你好，这是一个测试。',
}

BAD = [
    {'text': 'x', 'speed': 0.01},
    {'text': 'x', 'speed': 'fast'},
    {'text': 123},
    {'text': 'x', 'lang': 'xx'},
    {'text': 'x', 'voice': 'nope'},
    {'text': 'Bonjour', 'lang': 'f', 'voice': 'am_adam'},
]


def call(path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def generate(lang, text):
    t0 = time.time()
    code, body = call('/api/jobs', {'text': text, 'lang': lang})
    if code not in (200, 202):
        raise AssertionError(f'submit {code}: {body[:200]!r}')
    job = json.loads(body)
    while job['status'] in ('queued', 'running'):
        if time.time() - t0 > 600:
            raise AssertionError('timed out after 600 s')
        time.sleep(1)
        code, body = call(f"/api/jobs/{job['id']}")
        if code != 200:
            raise AssertionError(f'poll {code}: {body[:200]!r}')
        job = json.loads(body)
    if job['status'] != 'done':
        raise AssertionError(job.get('error', job))
    code, audio = call(job['audio_url'])
    if code != 200 or not audio.startswith(b'RIFF'):
        raise AssertionError(f'audio {code}')
    with wave.open(io.BytesIO(audio)) as w:
        seconds = w.getnframes() / w.getframerate()
    if seconds < 0.3:
        raise AssertionError(f'only {seconds:.2f}s of audio')
    return time.time() - t0, seconds, job['cache']


def main():
    failures = 0
    code, body = call('/health')
    print(f'health {code} {body.decode()}')
    failures += code != 200

    for lang, text in SAMPLES.items():
        try:
            took, seconds, cache = generate(lang, text)
            print(f'ok   {lang}  {seconds:4.1f}s audio in {took:5.1f}s ({cache})')
        except Exception as e:
            failures += 1
            print(f'FAIL {lang}  {e}')

    for body in BAD:
        code, resp = call('/api/tts', body)
        ok = code == 400
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {code} for {json.dumps(body, ensure_ascii=False)}")

    print('PASS' if not failures else f'{failures} FAILED')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
