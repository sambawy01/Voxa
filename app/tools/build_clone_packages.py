"""Build the two voice-cloning packages: the cloning model and the consent check.

    python -I app/tools/build_clone_packages.py pocket <sherpa-pocket-dir> <out-dir>
    python -I app/tools/build_clone_packages.py consent <whisper-tiny.en-dir> <campplus.onnx> <out-dir>

pocket:  sherpa-onnx-pocket-tts-int8-*.tar.bz2 from
         https://github.com/k2-fsa/sherpa-onnx/releases/tag/tts-models (Kyutai Pocket TTS,
         ONNX export by KevinAHM, CC-BY-4.0). The archive's test_wavs/ are not copied (they
         are third-party recordings, not part of the model). Kyutai's prohibited-use notice
         goes into RESTRICTIONS.txt, which the catalogue entry's license.restrictions should
         carry so the app shows it.
consent: sherpa-onnx-whisper-tiny.en (MIT) and 3dspeaker_speech_campplus_sv_en_voxceleb_16k
         (3D-Speaker, Apache-2.0) from the sherpa-onnx asr-models and
         speaker-recongition-models releases.
"""
import shutil
import sys
from pathlib import Path

POCKET_FILES = ('lm_flow.int8.onnx', 'lm_main.int8.onnx', 'encoder.onnx', 'decoder.int8.onnx',
                'text_conditioner.onnx', 'vocab.json', 'token_scores.json')

# Verbatim from https://huggingface.co/kyutai/pocket-tts (the gated model's terms) and
# https://github.com/kyutai-labs/pocket-tts (README, "Prohibited use").
POCKET_PROHIBITED = (
    'Prohibited use\n\n'
    'Use of our model must comply with all applicable laws and regulations and must not '
    'result in, involve, or facilitate any illegal, harmful, deceptive, fraudulent, or '
    'unauthorized activity. Prohibited uses include, without limitation, voice impersonation '
    'or cloning without explicit and lawful consent; misinformation, disinformation, or '
    'deception (including fake news, fraudulent calls, or presenting generated content as '
    'genuine recordings of real people or events); and the generation of unlawful, harmful, '
    'libelous, abusive, harassing, discriminatory, hateful, or privacy-invasive content. We '
    'disclaim all liability for any non-compliant use.\n')


def need(paths):
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        sys.exit('missing: ' + ', '.join(missing))


def pocket(src: Path, out: Path) -> None:
    need([src / f for f in POCKET_FILES] + [src / 'LICENSE'])
    if 'Attribution 4.0 International' not in (src / 'LICENSE').read_text(encoding='utf-8'):
        sys.exit(f'{src}/LICENSE is not CC-BY-4.0; check the model licence before packaging')
    out.mkdir(parents=True, exist_ok=False)
    for f in POCKET_FILES:
        shutil.copy2(src / f, out / f)
    shutil.copy2(src / 'LICENSE', out / 'LICENSE-model-CC-BY-4.0')
    (out / 'RESTRICTIONS.txt').write_text(POCKET_PROHIBITED, encoding='utf-8')
    (out / 'SOURCE.txt').write_text(
        f'{src.name}: Pocket TTS by Kyutai (CC-BY-4.0), ONNX export by KevinAHM '
        '(CC-BY-4.0), int8 build by k2-fsa/sherpa-onnx (Apache-2.0). Repacked for Voxa.\n')
    print(f'{out}: Pocket TTS, {len(POCKET_FILES)} files')


def consent(whisper: Path, campplus: Path, out: Path) -> None:
    files = {
        'tiny.en-encoder.int8.onnx': whisper / 'tiny.en-encoder.int8.onnx',
        'tiny.en-decoder.int8.onnx': whisper / 'tiny.en-decoder.int8.onnx',
        'tiny.en-tokens.txt': whisper / 'tiny.en-tokens.txt',
        'campplus_en_voxceleb_16k.onnx': campplus,
    }
    need(files.values())
    out.mkdir(parents=True, exist_ok=False)
    for name, path in files.items():
        shutil.copy2(path, out / name)
    (out / 'SOURCE.txt').write_text(
        'Whisper tiny.en by OpenAI (MIT), ONNX export by k2-fsa/sherpa-onnx (Apache-2.0).\n'
        'CAM++ speaker embedding (VoxCeleb, English) by 3D-Speaker / Alibaba (Apache-2.0).\n'
        'Used only for Voxa\'s consent check before voice cloning. Repacked for Voxa.\n')
    print(f'{out}: consent check, {len(files)} files')


if __name__ == '__main__':
    a = sys.argv[1:]
    if a[:1] == ['pocket'] and len(a) == 3:
        pocket(Path(a[1]), Path(a[2]))
    elif a[:1] == ['consent'] and len(a) == 4:
        consent(Path(a[1]), Path(a[2]), Path(a[3]))
    else:
        sys.exit(__doc__)
