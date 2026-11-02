"""Build a Voxa voice package from sherpa-onnx's Supertonic 3 export.

    python -I app/tools/build_supertonic_package.py <sherpa-supertonic-dir> <out-dir> <model-licence>

<sherpa-supertonic-dir> is the extracted sherpa-onnx-supertonic-3-tts-int8-*.tar.bz2 from
https://github.com/k2-fsa/sherpa-onnx/releases/tag/tts-models. <model-licence> is the
model's licence, https://huggingface.co/Supertone/supertonic-3/blob/main/LICENSE
(BigScience Open RAIL-M). The archive's own LICENSE is the MIT licence of Supertone's sample
code. Both go into the package, and the use restrictions (Attachment A) go into
RESTRICTIONS.txt, which the catalogue entry's license.restrictions should carry so the app
can show them.
"""
import shutil
import sys
from pathlib import Path

FILES = ('duration_predictor.int8.onnx', 'text_encoder.int8.onnx', 'vector_estimator.int8.onnx',
         'vocoder.int8.onnx', 'tts.json', 'unicode_indexer.bin', 'voice.bin')


def restrictions(licence: str) -> str:
    """The text from the 'Attachment A' heading line to the end (the use restrictions).
    Matches the heading itself, not earlier sentences that mention Attachment A."""
    lines = licence.splitlines()
    starts = [i for i, l in enumerate(lines) if l.strip() == 'Attachment A']
    if not starts or 'Open RAIL-M' not in licence:
        sys.exit('the model licence does not look like Open RAIL-M (no Attachment A heading)')
    text = '\n'.join(lines[starts[-1]:]).strip() + '\n'
    if 'You agree not to use the Model' not in text:
        sys.exit('Attachment A found, but it does not contain the use restrictions')
    return text


def main(src: Path, out: Path, model_licence: Path) -> None:
    missing = [f for f in FILES if not (src / f).is_file()]
    if missing:
        sys.exit(f'{src} is missing {", ".join(missing)}')
    text = model_licence.read_text(encoding='utf-8')
    rules = restrictions(text)
    out.mkdir(parents=True, exist_ok=False)
    for f in FILES:
        shutil.copy2(src / f, out / f)
    shutil.copy2(src / 'LICENSE', out / 'LICENSE-code-MIT')
    (out / 'LICENSE-model-OpenRAIL-M').write_text(text, encoding='utf-8')
    (out / 'RESTRICTIONS.txt').write_text(rules, encoding='utf-8')
    (out / 'SOURCE.txt').write_text(
        f'{src.name}: Supertonic 3 by Supertone Inc. (model: Open RAIL-M; sample code: MIT),\n'
        'ONNX export by sherpa-onnx (Apache-2.0, k2-fsa). Repacked for Voxa.\n')
    print(f'{out}: Supertonic 3, {len(FILES)} model files, restrictions {len(rules)} chars')


if __name__ == '__main__':
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
