"""Download SmollerTTS for the browser.

    python scripts/build_smoller.py [static-dir]      # default: ./static

SmollerTTS (Dun Li Chan, MIT; 2.8M parameters, one English voice trained on LJSpeech, which is
public domain) ships its own browser runtime on Hugging Face: a worker, its English front end
(pronunciation data adapted from the MFA English dictionary, CC BY 4.0), a HiFi-GAN vocoder
(MIT) and onnxruntime-web (MIT). It is kept exactly as released (the worker checks every
file's SHA-256 itself), so the browser loads all of it from this site. Licence files are
renamed to .txt so the site can serve them.

Every file is pinned by revision and checked against the release's SHA256SUMS, itself pinned.
"""
import hashlib
import os
import shutil
import sys
import tempfile
import urllib.request

REPO = 'dunli5107/SmollerTTS'
REVISION = 'a4b7e512938dabffc2e0f6fc5d14dc43882a1599'
DIR = 'smollertts-' + REVISION[:7]
SUMS_SHA256 = '78162720a468b78b3dd196cdb84ee15a7da9c0121c5b44b7e6b50d0577d4fa4e'
RUNTIME = ('worker.mjs', 'engine.mjs', 'frontend-v27.bundle.mjs', 'lexicon.mjs', 'controls-v19.mjs',
           'audio.mjs', 'punctuation.mjs', 'assets.json', 'frontend.json', 'en.lex',
           'encoder.onnx', 'frames.onnx', 'vocoder.onnx',
           'ort/ort.wasm.min.mjs', 'ort/ort-wasm-simd-threaded.mjs', 'ort/ort-wasm-simd-threaded.wasm')
NOTICES = ('LICENSE', 'HIFIGAN-LICENSE', 'ONNXRUNTIME-LICENSE', 'PHONEMIZE-LICENSE', 'TO-WORDS-LICENSE',
           'THIRD_PARTY.md')


def fetch(name, dest):
    url = f'https://huggingface.co/{REPO}/resolve/{REVISION}/{name}'
    path = os.path.join(dest, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with urllib.request.urlopen(url, timeout=600) as r, open(path, 'wb') as out:
        while chunk := r.read(1 << 20):
            out.write(chunk)
    with open(path, 'rb') as f:
        return path, hashlib.file_digest(f, 'sha256').hexdigest()


def main(static_dir):
    out = os.path.join(static_dir, DIR)
    if os.path.isdir(out):
        print(f'{out}: already built')
        return
    with tempfile.TemporaryDirectory() as tmp:
        build = os.path.join(tmp, DIR)
        sums_path, got = fetch('SHA256SUMS', tmp)
        if got != SUMS_SHA256:
            sys.exit('SHA256SUMS: checksum mismatch, refusing to build')
        with open(sums_path, encoding='utf-8') as f:
            sums = {line.split()[1]: line.split()[0] for line in f if line.strip()}
        for name in RUNTIME + NOTICES:
            path, got = fetch(name, build)
            if sums.get(name) != got:
                sys.exit(f'{name}: not in the release checksums, refusing to build')
        for name in NOTICES:  # served as text, so the licences can be linked from the site
            base = os.path.splitext(name)[0]
            os.rename(os.path.join(build, name), os.path.join(build, base + '.txt'))
        with open(os.path.join(build, 'SOURCE.txt'), 'w', encoding='utf-8') as f:
            f.write(f'SmollerTTS by Dun Li Chan (https://huggingface.co/{REPO}, revision {REVISION}), MIT. '
                    'Third-party notices in THIRD_PARTY.txt and the *-LICENSE.txt files. Unmodified.\n')
        os.makedirs(static_dir, exist_ok=True)
        shutil.move(build, out)
    total = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(out) for f in fs)
    print(f'{out}: {total / 1e6:.0f} MB')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'static'))
