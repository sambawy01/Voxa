"""Download the phonon-tts browser package, check it, and unpack it for Voxa to serve.

    python scripts/fetch_phonon.py [static-dir]      # default: ./static

Voxa serves the package itself rather than from a CDN: its worker must be same-origin, and
the page runs cross-origin isolated (for multi-threaded WebAssembly). The version and its
npm integrity hash are pinned here; a tarball that does not match is refused.
"""
import base64
import hashlib
import io
import os
import sys
import tarfile
import urllib.request

VERSION = '0.4.1'
INTEGRITY = ('sha512-1T/EBS5FcWjpZeVotTE0Pftgqqhr0D5IWHEa9TtH0EMbba5XpuTWo+hn0N29lb5b7Vi3Dpp'
             '+p/Sb/6z4Uf/1Cw==')
TARBALL = f'https://registry.npmjs.org/phonon-tts/-/phonon-tts-{VERSION}.tgz'
# Only what the browser loads; the type declarations and README stay behind.
SUFFIXES = ('.js', '.wasm')


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), '..', 'static')
    dest = os.path.join(os.path.abspath(root), f'phonon-{VERSION}')
    with urllib.request.urlopen(TARBALL, timeout=120) as r:
        data = r.read()
    algo, expected = INTEGRITY.split('-', 1)
    actual = base64.b64encode(hashlib.new(algo, data).digest()).decode()
    if actual != expected:
        sys.exit(f'phonon-tts {VERSION}: integrity mismatch, refusing to install')

    count = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar:
        for member in tar.getmembers():
            name = member.name
            if not member.isfile() or not name.startswith('package/') or not name.endswith(SUFFIXES):
                continue
            rel = os.path.normpath(name[len('package/'):])
            if rel.startswith('..') or os.path.isabs(rel):
                continue
            target = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with tar.extractfile(member) as src, open(target, 'wb') as out:
                out.write(src.read())
            count += 1
    print(f'phonon-tts {VERSION}: {count} files in {dest}')


if __name__ == '__main__':
    main()
