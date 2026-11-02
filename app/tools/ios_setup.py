"""Prepare the iOS project that `tauri ios init` generates, for Voxa.

    python -I app/tools/ios_setup.py <app/src-tauri/gen/apple>

Sets NSMicrophoneUsageDescription in the app's Info.plist (idempotent). iOS ends an app
that opens the microphone without it, and "Your voices" records the consent sentence and
voice sample live.

    python -I app/tools/ios_setup.py --self-test
"""
import plistlib
import sys
from pathlib import Path

MIC = ('Voxa records your consent and a voice sample when you make one of Your voices. '
       'Recordings stay on this device.')


def configure(plist: dict) -> dict:
    out = dict(plist)
    out['NSMicrophoneUsageDescription'] = MIC
    return out


def info_plists(project: Path) -> list:
    """The iOS app's Info.plist files (one per app target, e.g. voxa_iOS/Info.plist)."""
    return sorted(p for p in project.glob('*_iOS/Info.plist') if p.is_file())


def self_test() -> None:
    base = {'CFBundleIdentifier': 'app.voxa.desktop', 'CFBundleName': 'Voxa'}
    out = configure(base)
    assert out['NSMicrophoneUsageDescription'] == MIC
    assert out['CFBundleIdentifier'] == 'app.voxa.desktop', 'keeps other keys'
    assert configure(out) == out, 'idempotent'
    data = plistlib.dumps(out)
    assert plistlib.loads(data)['NSMicrophoneUsageDescription'] == MIC, 'round trip'
    print('ios_setup self-test ok')


def main(project: Path) -> None:
    plists = info_plists(project)
    if not plists:
        sys.exit(f'no *_iOS/Info.plist under {project}; run `tauri ios init` first')
    for p in plists:
        with p.open('rb') as f:
            data = plistlib.load(f)
        with p.open('wb') as f:
            plistlib.dump(configure(data), f)
        print(f'{p}: NSMicrophoneUsageDescription set')


if __name__ == '__main__':
    if sys.argv[1:] == ['--self-test']:
        self_test()
    elif len(sys.argv) == 2:
        main(Path(sys.argv[1]))
    else:
        sys.exit(__doc__)
