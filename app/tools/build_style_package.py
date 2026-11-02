"""Build a Voxa voice package from a sherpa-onnx Kokoro or Kitten release.

    python -I app/tools/build_style_package.py <sherpa-model-dir> <out-dir> [<misaki-data-dir>]

<sherpa-model-dir> is an extracted archive from
https://github.com/k2-fsa/sherpa-onnx/releases/tag/tts-models, e.g.
kokoro-int8-multi-lang-v1_0 or kitten-nano-en-v0_8-int8. The model family is read from the
model's own metadata.

The package keeps only what Voxa's engine uses: the model, voices.bin, tokens.txt, the
licence, and speakers.json, written from the model's own metadata (speaker_names) so speaker
ids never depend on a hard-coded list. espeak-ng-data (GPL) and the Chinese text-processing
files are left out on purpose: Voxa phonemizes with its own G2P.

With <misaki-data-dir> (misaki's misaki/data, holding us_gold.json and us_silver.json, Apache-2.0)
the English dictionary for Voxa's G2P is added under g2p/ with misaki's licence.

Needs: pip install onnx (reads metadata only; the weights are not loaded).
"""
import json
import shutil
import sys
from pathlib import Path


def speaker_names(model: Path) -> list[str]:
    import onnx

    m = onnx.load(str(model), load_external_data=False)
    meta = {p.key: p.value for p in m.metadata_props}
    if meta.get('model_type') not in ('kokoro', 'kitten-tts'):
        sys.exit(f'{model} is not a Kokoro or Kitten model (model_type={meta.get("model_type")!r})')
    names = meta.get('speaker_names', '').split(',')
    if not names or names != [n.strip() for n in names] or len(names) != int(meta['n_speakers']):
        sys.exit('speaker_names metadata is missing or inconsistent')
    return names


def add_g2p(misaki: Path, out: Path) -> None:
    g2p = out / 'g2p'
    g2p.mkdir()
    for name in ('us_gold.json', 'us_silver.json'):
        data = json.loads((misaki / name).read_text(encoding='utf-8'))
        if len(data) < 50_000:
            sys.exit(f'{name} looks truncated ({len(data)} entries)')
        shutil.copy2(misaki / name, g2p / name)
    licence = misaki / 'LICENSE'
    if not licence.is_file():
        licence = misaki.parent.parent / 'LICENSE'
    if not licence.is_file():
        sys.exit('misaki LICENSE not found next to the data or at the repo root')
    shutil.copy2(licence, g2p / 'LICENSE')
    (g2p / 'SOURCE.txt').write_text('English pronunciation dictionary from misaki (Apache-2.0, hexgrad/misaki).\n')


def main(src: Path, out: Path, misaki: Path | None = None) -> None:
    model = next((src / n for n in ('model.int8.onnx', 'model.onnx') if (src / n).is_file()), None)
    if model is None:
        sys.exit(f'no model.int8.onnx or model.onnx in {src}')
    out.mkdir(parents=True, exist_ok=False)
    for name in (model.name, 'voices.bin', 'tokens.txt', 'LICENSE'):
        shutil.copy2(src / name, out / name)
    names = speaker_names(model)
    (out / 'speakers.json').write_text(json.dumps(names))
    (out / 'SOURCE.txt').write_text(
        f'{src.name}: ONNX export and voices.bin by sherpa-onnx (Apache-2.0, k2-fsa) of\n'
        'Kokoro-82M (Apache-2.0, hexgrad) or Kitten TTS (Apache-2.0, KittenML).\n'
        'Repacked for Voxa without espeak-ng-data; Voxa phonemizes with its own G2P.\n'
    )
    if misaki is not None:
        add_g2p(misaki, out)
    print(f'{out}: {model.name}, {len(names)} speakers' + (', with g2p/' if misaki else ''))


if __name__ == '__main__':
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]) if len(sys.argv) == 4 else None)
