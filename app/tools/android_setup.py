"""Prepare the Android project that `tauri android init` generates, for Voxa.

    python -I app/tools/android_setup.py <app/src-tauri/gen/android>

Adds the permissions Voxa needs to app/src/main/AndroidManifest.xml (idempotent):
- RECORD_AUDIO: live recordings for "Your voices" (consent sentence and voice sample).
- MODIFY_AUDIO_SETTINGS: lets the webview's getUserMedia route the microphone.
INTERNET is already in Tauri's template (model catalogue downloads, licence activation).

    python -I app/tools/android_setup.py --self-test
"""
import re
import sys
from pathlib import Path

PERMISSIONS = ('android.permission.RECORD_AUDIO', 'android.permission.MODIFY_AUDIO_SETTINGS')


def add_permissions(manifest: str) -> str:
    if '<manifest' not in manifest:
        raise ValueError('not an AndroidManifest.xml (no <manifest> element)')
    missing = [p for p in PERMISSIONS if f'android:name="{p}"' not in manifest]
    if not missing:
        return manifest
    lines = ''.join(f'\n    <uses-permission android:name="{p}" />' for p in missing)
    # Insert right after the opening <manifest ...> tag, which may span several lines.
    m = re.search(r'<manifest\b[^>]*>', manifest)
    return manifest[:m.end()] + lines + manifest[m.end():]


def self_test() -> None:
    template = ('<?xml version="1.0" encoding="utf-8"?>\n'
                '<manifest xmlns:android="http://schemas.android.com/apk/res/android"\n'
                '    xmlns:tools="http://schemas.android.com/tools">\n'
                '    <uses-permission android:name="android.permission.INTERNET" />\n'
                '    <application android:label="Voxa" />\n</manifest>\n')
    out = add_permissions(template)
    for p in PERMISSIONS:
        assert out.count(f'android:name="{p}"') == 1, p
    assert out.index('RECORD_AUDIO') > out.index('xmlns:tools'), 'inside <manifest>'
    assert add_permissions(out) == out, 'idempotent'
    try:
        add_permissions('<html/>')
    except ValueError:
        pass
    else:
        raise AssertionError('accepted a non-manifest')
    print('android_setup self-test ok')


def main(project: Path) -> None:
    manifest = project / 'app' / 'src' / 'main' / 'AndroidManifest.xml'
    if not manifest.is_file():
        sys.exit(f'{manifest} not found; run `tauri android init` first')
    text = manifest.read_text(encoding='utf-8')
    manifest.write_text(add_permissions(text), encoding='utf-8')
    print(f'{manifest}: permissions {", ".join(p.rsplit(".", 1)[1] for p in PERMISSIONS)}')


if __name__ == '__main__':
    if sys.argv[1:] == ['--self-test']:
        self_test()
    elif len(sys.argv) == 2:
        main(Path(sys.argv[1]))
    else:
        sys.exit(__doc__)
