"""Download Kale TTS nano for the browser.

    python scripts/build_kale.py [static-dir]      # default: ./static

Kale TTS nano (KaleComputer, Apache-2.0; training data CC BY 4.0; ten designed voices, none a
real person) is a 7.5M-parameter English voice. Its own browser runtime (js/kale-tts.mjs) and
English front end (frontend/: lexicon, letter-to-sound model, number and abbreviation reading;
no espeak) are kept as released, laid out the way kale-tts.mjs expects, so the browser loads it
all from this site:

    js/kale-tts.mjs, frontend/..., model/{voices.json, tokenizer.json, kale-tts-1-nano.onnx, voices/*}

Only the int8 graph is kept (the WebAssembly path). Every file is pinned by revision and
SHA-256: model files by the release's own SHA256SUMS (itself pinned here), the rest below.
"""
import hashlib
import os
import shutil
import sys
import tempfile
import urllib.request

REPO = 'KaleComputer/kale-tts-1-nano'
REVISION = 'b6ce1887268c3667994fc3b0b2c82887c069915c'
DIR = 'kale-nano-' + REVISION[:7]
VOICES = ('s057b', 'f06', 'd03', 'w165', 'w602', 'm3', 't071', 'd13', 'x328', 'h013f')
PINNED = {
    'SHA256SUMS': '243d78093e65e8023528daa8784b2e2ac0550a270d9937bcf2a7869b24bf8d66',
    'js/kale-tts.mjs': 'f4e9916197be000d5909035682da83051fb767ecb4d5162d89b77f23f9bb0039',
    'frontend/words/kaleTts.js': '4d52bfca1176afa867cbc812b4d864e56561e08b51219e624b43a6d2ed3bbae5',
    'frontend/ettin.js': '53a50173f4131df186462a9b1d56a66df27c569b6e35d5cc2c82d9077da46135',
    'frontend/dizione/spoken.js': '2ba64d8740c3d30efc04e6f964f4e5e3342dc93671b0d3f0feabf20c2a44cdfd',
    'frontend/dizione/tables.js': '7fa8e0f95c3080e28946534fba42b4008905568c2eae553359b49aecc65380c6',
    'frontend/dizione/homographs.js': 'dfb3e1f170d18b31053609a3ce944919105b65678a441804f053a9fae22d9125',
    'frontend/dizione/homographs.data.js': '1dc45e2e5ca7ee6d4b6035f1fb66f4c5b0c0e9bce7f0ce6cb5468481989a1cb0',
    'frontend/dizione/LICENSE': 'cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30',
    'frontend/words/g2p.bin': 'aba532e5e07fb6887e78148957ab29c890374ead77bf555c247d99b2dc0f51f5',
    'frontend/words/lexicon.tsv': '1eec6681dfbc213660f78548c14894e7de6dad6ef73caaac279f2ed400a91b7c',
    'frontend/words/override.public.tsv': '0d725b31afd09a33a9908ea889be2d65181cda16c89a6946cfd2b9b0b0220b22',
    'frontend/words/terms.tsv': '987ce777805e22718e38d23e66d8f00e9509ede28d0d52cc6819d7216e798e3d',
    # the British front end, which the two British voices (Amy, Jack) read through
    'frontend/release-loader.mjs': '5b8becfec5d45a05ff1137ae41ca0fa51af2a466fce3b43bd53007acfd0bfe39',
    'frontend/words/enGb.js': 'cecd4c958b5fec438267a01b475fbad31acb822371a14d130eea25671d9477a0',
    'frontend/words/en-gb/lexicon.tsv': '88565f7eb7de4a9848332018ad19cc121572d7a8257834263648eedeae01f5e7',
    'frontend/words/en-gb/override.public.tsv': '6f4105e439fe5b831c2c8eb8d3fb806524ed339094d0208824a833eb24a95103',
    'frontend/words/en-gb/terms.tsv': '987ce777805e22718e38d23e66d8f00e9509ede28d0d52cc6819d7216e798e3d',
    'frontend/words/en-gb/g2p.bin': '21e3753cc506d4bb7ead258dddb61b8c462528f6e0a154f24a89799ad96c9a18',
    'LICENSE': 'cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30',
    'THIRD_PARTY_NOTICES.md': 'd732919e8cacdfd200cf11a61c12cc8c272e103bf235f060aa99a339a92b8474',
}
MODEL_FILES = ('voices.json', 'tokenizer.json', 'kale-tts-1-nano.onnx',
               *(f'voices/{v}{ext}' for v in VOICES for ext in ('.bin', '.extra.bin')))


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
        for name, want in PINNED.items():
            _, got = fetch(name, build)
            if got != want:
                sys.exit(f'{name}: checksum mismatch, refusing to build')
        with open(os.path.join(build, 'SHA256SUMS'), encoding='utf-8') as f:
            sums = {line.split()[1]: line.split()[0] for line in f if line.strip()}
        for name in MODEL_FILES:
            path, got = fetch(name, os.path.join(build, 'model'))
            if sums.get(name) != got:
                sys.exit(f'{name}: not in the release checksums, refusing to build')
        os.remove(os.path.join(build, 'SHA256SUMS'))
        # served as text, so the licences can be linked from the site
        os.rename(os.path.join(build, 'LICENSE'), os.path.join(build, 'LICENSE.txt'))
        os.rename(os.path.join(build, 'frontend/dizione/LICENSE'), os.path.join(build, 'frontend/dizione/LICENSE.txt'))
        os.rename(os.path.join(build, 'THIRD_PARTY_NOTICES.md'), os.path.join(build, 'THIRD_PARTY_NOTICES.txt'))
        with open(os.path.join(build, 'SOURCE.txt'), 'w', encoding='utf-8') as f:
            f.write(f'Kale TTS nano by KaleComputer (https://huggingface.co/{REPO}, revision {REVISION}), '
                    'Apache-2.0. Third-party notices in THIRD_PARTY_NOTICES.txt. Unmodified; int8 graph only.\n')
        os.makedirs(static_dir, exist_ok=True)
        shutil.move(build, out)
    total = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(out) for f in fs)
    print(f'{out}: {total / 1e6:.0f} MB')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'static'))
