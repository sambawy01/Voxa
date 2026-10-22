"""Download the browser packages Voxa serves itself, check them, and unpack them.

    python scripts/fetch_vendor.py [static-dir]      # default: ./static

Voxa serves these from its own origin rather than a CDN: their workers must be same-origin,
and the pages run cross-origin isolated (for multi-threaded WebAssembly). Each package is
pinned by version and npm integrity hash; a tarball that does not match is refused.
"""
import base64
import hashlib
import io
import os
import re
import sys
import tarfile
import urllib.request

PACKAGES = [
    {   # Voxa Lite: Phonon text-to-speech in the browser. MIT OR Apache-2.0.
        'name': 'phonon-tts', 'version': '0.4.1', 'dir': 'phonon-0.4.1',
        'integrity': 'sha512-1T/EBS5FcWjpZeVotTE0Pftgqqhr0D5IWHEa9TtH0EMbba5XpuTWo+hn0N29lb5b7Vi3Dpp'
                     '+p/Sb/6z4Uf/1Cw==',
        'keep': r'[^/].*\.(js|wasm)',
    },
    {   # Audiobooks: PDF text extraction. Apache-2.0. Character maps cover CJK and other
        # PDFs whose fonts map glyphs through a CMap.
        'name': 'pdfjs-dist', 'version': '4.4.168', 'dir': 'pdfjs-4.4.168',
        'integrity': 'sha512-MbkAjpwka/dMHaCfQ75RY1FXX3IewBVu6NGZOcxerRFlaBiIkZmUoR0jotX5VUzYZEXAGzSFtknWs5xRKliXPA==',
        'keep': r'build/pdf(\.worker)?\.min\.mjs|cmaps/[^/]+\.bcmap|LICENSE',
    },
]


def fetch(pkg, root):
    url = f'https://registry.npmjs.org/{pkg["name"]}/-/{pkg["name"]}-{pkg["version"]}.tgz'
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read()
    algo, expected = pkg['integrity'].split('-', 1)
    if base64.b64encode(hashlib.new(algo, data).digest()).decode() != expected:
        sys.exit(f'{pkg["name"]} {pkg["version"]}: integrity mismatch, refusing to install')
    dest = os.path.join(root, pkg['dir'])
    count = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.startswith('package/'):
                continue
            rel = os.path.normpath(member.name[len('package/'):])
            if rel.startswith('..') or os.path.isabs(rel) or not re.fullmatch(pkg['keep'], rel):
                continue
            target = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with tar.extractfile(member) as src, open(target, 'wb') as out:
                out.write(src.read())
            count += 1
    print(f'{pkg["name"]} {pkg["version"]}: {count} files in {dest}')


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), '..', 'static')
    for pkg in PACKAGES:
        fetch(pkg, os.path.abspath(root))


if __name__ == '__main__':
    main()
