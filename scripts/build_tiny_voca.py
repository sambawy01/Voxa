"""Build the browser files for tiny-voca, the 4.5M English voice (Apache-2.0), into a folder.

    python scripts/build_tiny_voca.py [static-dir]      # default: ./static

Everything is pinned and checked: the model files by commit and SHA-256, Pyodide (a Python
that runs in the browser, which the phonemizer needs) by npm integrity, and its NumPy build
by SHA-256. A file that does not match is refused. Voxa serves them itself, so a listener's
browser never contacts a third party.

Result, in static/TINY_VOCA_DIR (the same name as server.TINYVOCA_DIR):
    model.onnx, config.json     the voice
    phonemizer.zip              the model's lexicon and G2P data (its phonemizer/ folder)
    dizione.zip                 the phonemizer's code, from vendor/dizione
    pyodide/                    Pyodide core and NumPy
"""
import base64
import hashlib
import io
import os
import re
import sys
import tarfile
import urllib.request
import zipfile

REVISION = 'd94d2a0338fd1e0eab7dd896ec0ad23d8cdf7c14'
OUT_DIR = 'tiny-voca-d94d2a0'
HF = f'https://huggingface.co/n4ze3m/tiny-voca/resolve/{REVISION}/'
MODEL_FILES = {
    'onnx/model.onnx': 'fe6d918791a15b82e37465981d7f27a070e46b46635e5a3482fc8dff87a1ae46',
    'config.json': '27bcbf322fc428581f0e959be1c141dd7751c61f4fed5eded326e779b215f69c',
}
PHONEMIZER_FILES = {
    'LICENSE.cmudict': 'bd4ce8e44170a5f9f481310ca85c51de3c4f851a65e679b40e603b143bd3542a',
    'LICENSE.homograph-data': 'cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30',
    'align.json': '864763dbd558a864950266b2c30627f1cad203e75062ee842ee63c653e823b75',
    'g2p.json': 'c518fd839ea0109c0b2b4f778c17506bf0d7653557b8b8c97a4c8dd9883d4432',
    'g2p.npz': 'eb3fd519ff00e72d850928d2fccb968adc36134bca2f3bda09de7e794c2ed01b',
    'homographs.json': '9c3a96fcdf79fa27d1b3686dc6a507c37e4f754966928608ee9ddb617ba39192',
    'lexicon.json': '4d67c1f94b86405a5b8766fb13b8cd92a1371204767dd709582a337e8e0ab334',
    'lexicon.tsv': '3fc930345c6b3cabc533981c4b212fa309eec83913fcdbd42919338270d64b06',
    'sources.json': 'f4c54924d30a5bc0bc1e3d7b058adb924484b3fab8541bc8d8bcecfb028fdbad',
}
PYODIDE = {   # Pyodide 0.26.4 (MPL-2.0): the core from npm, NumPy (BSD-3-Clause) from its CDN
    'version': '0.26.4',
    'integrity': 'sha512-z2CHsjVlhhJi5tYBF0AYAfNEPo3zq/z+xOpFtk1tweJkRaTqU4UK/7pLvo8DBU2VDPH31vB3pSI+8fnoqrVrFg==',
    'keep': r'(pyodide\.js|pyodide\.asm\.js|pyodide\.asm\.wasm|python_stdlib\.zip|pyodide-lock\.json|LICENSE)',
    'numpy': 'numpy-1.26.4-cp312-cp312-pyodide_2024_0_wasm32.whl',
    'numpy_sha256': '4a2f5303a88a0747c5e6c80701e0ff4ff5667e487c0c9f9c73875e4d88f4cf9a',
}
HERE = os.path.dirname(os.path.abspath(__file__))


def download(url):
    with urllib.request.urlopen(url, timeout=300) as r:
        return r.read()


def checked(data, sha256, what):
    if hashlib.sha256(data).hexdigest() != sha256:
        sys.exit(f'{what}: checksum mismatch, refusing to install')
    return data


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)


def pack(files):
    """A zip (deflated, so it travels smaller) of {name: bytes}; fixed timestamps keep it reproducible."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in sorted(files):
            z.writestr(zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0)), files[name], zipfile.ZIP_DEFLATED, 9)
    return buf.getvalue()


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, '..', 'static')
    dest = os.path.join(os.path.abspath(root), OUT_DIR)
    for name, sha in MODEL_FILES.items():
        write(os.path.join(dest, os.path.basename(name)), checked(download(HF + name), sha, name))
    data = {name: checked(download(HF + 'phonemizer/' + name), sha, name) for name, sha in PHONEMIZER_FILES.items()}
    write(os.path.join(dest, 'phonemizer.zip'), pack(data))

    code, base = {}, os.path.join(HERE, '..', 'vendor', 'dizione')
    for folder, _, names in os.walk(os.path.join(base, 'dizione')):
        for name in names:
            if name.endswith(('.py', '.txt')):
                with open(os.path.join(folder, name), 'rb') as f:
                    code[os.path.relpath(os.path.join(folder, name), base)] = f.read()
    write(os.path.join(dest, 'dizione.zip'), pack(code))

    url = f'https://registry.npmjs.org/pyodide/-/pyodide-{PYODIDE["version"]}.tgz'
    algo, expected = PYODIDE['integrity'].split('-', 1)
    tgz = download(url)
    if base64.b64encode(hashlib.new(algo, tgz).digest()).decode() != expected:
        sys.exit('pyodide: integrity mismatch, refusing to install')
    with tarfile.open(fileobj=io.BytesIO(tgz), mode='r:gz') as tar:
        for member in tar.getmembers():
            rel = os.path.normpath(member.name[len('package/'):]) if member.name.startswith('package/') else ''
            if member.isfile() and re.fullmatch(PYODIDE['keep'], rel):
                write(os.path.join(dest, 'pyodide', rel), tar.extractfile(member).read())
    wheel = PYODIDE['numpy']
    write(os.path.join(dest, 'pyodide', wheel), checked(
        download(f'https://cdn.jsdelivr.net/pyodide/v{PYODIDE["version"]}/full/{wheel}'), PYODIDE['numpy_sha256'], wheel))
    total = sum(os.path.getsize(os.path.join(f, n)) for f, _, ns in os.walk(dest) for n in ns)
    print(f'tiny-voca: {total / 1e6:.0f} MB in {dest}')


if __name__ == '__main__':
    main()
