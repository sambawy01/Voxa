"""Build a Voxa Studio (Qwen3-TTS) worker package.

    python -I app/tools/build_studio_package.py <qwen3-model-dir> <out-dir> [--env <python-env>] [--link-env]

<qwen3-model-dir> is a Qwen3-TTS-12Hz CustomVoice download, 0.6B or 1.7B (Apache-2.0):
https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice

The pack runs app/workers/qwen3_tts_worker.py with a Python that has `qwen-tts` and
PyTorch installed. Without --env the command is the system `python3`; with --env the
given environment is copied into the pack as env/ and used, so the pack needs nothing on
the machine. These environments are per platform: CUDA on Windows and Linux, MPS on
macOS. --link-env symlinks the environment instead of copying it (local testing only).

Then sign it: voxa-catalogue pack <out-dir> <entry.json> <secret-key>, with
"engine": "worker" and "tier": "gpu" in the entry.
"""
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKERS = HERE.parent / 'workers'
MODEL_FILES = ('config.json', 'generation_config.json', 'merges.txt', 'model.safetensors',
               'preprocessor_config.json', 'tokenizer_config.json', 'vocab.json')

# Voxa voice ids (as in qwen3_tts_worker.VOICES) -> display name, gender, language tag.
VOICES = [
    ('studio-ryan', 'Ryan', 'male', 'en-US'),
    ('studio-aiden', 'Aiden', 'male', 'en-US'),
    ('studio-vivian', 'Vivian', 'female', 'zh'),
    ('studio-serena', 'Serena', 'female', 'zh'),
    ('studio-uncle-fu', 'Uncle Fu', 'male', 'zh'),
    ('studio-dylan', 'Dylan', 'male', 'zh'),
    ('studio-eric', 'Eric', 'male', 'zh'),
    ('studio-ono-anna', 'Ono Anna', 'female', 'ja'),
    ('studio-sohee', 'Sohee', 'female', 'ko'),
]


def env_python(env: Path) -> str:
    for rel in ('bin/python3', 'bin/python', 'python.exe', 'Scripts/python.exe'):
        if (env / rel).exists():
            return rel
    sys.exit(f'{env} has no python (bin/python3 or python.exe)')


def manifest(command: list) -> dict:
    return {
        'protocol': 1,
        'family': 'Voxa Studio',
        'command': command,
        'voices': [{'id': i, 'name': n, 'gender': g, 'language': l} for i, n, g, l in VOICES],
        # Loading 1.7B from disk takes a while on first use; a sentence on a GPU is ~1 s.
        'start_timeout_s': 600,
        'request_timeout_s': 600,
    }


def main(model: Path, out: Path, env: Path = None, link_env: bool = False) -> None:
    missing = [f for f in MODEL_FILES if not (model / f).is_file()]
    if missing or not (model / 'speech_tokenizer').is_dir():
        sys.exit(f'{model} is not a Qwen3-TTS CustomVoice download (missing {missing or "speech_tokenizer/"})')
    out.mkdir(parents=True, exist_ok=False)
    (out / 'model').mkdir()
    for f in MODEL_FILES:
        shutil.copy2(model / f, out / 'model' / f)
    shutil.copytree(model / 'speech_tokenizer', out / 'model' / 'speech_tokenizer')
    for f in ('voxa_worker.py', 'qwen3_tts_worker.py'):
        shutil.copy2(WORKERS / f, out / f)
    worker = ['qwen3_tts_worker.py', '--model', 'model']
    if env:
        rel = env_python(env)
        if link_env:
            os.symlink(env.resolve(), out / 'env')
        else:
            shutil.copytree(env, out / 'env', symlinks=True)
        command = [f'./env/{rel}', '-I'] + worker
    else:
        command = ['python3', '-I'] + worker
    (out / 'worker.json').write_text(json.dumps(manifest(command), indent=2) + '\n')
    (out / 'SOURCE.txt').write_text(
        f'{model.name}: Qwen3-TTS by the Qwen team, Alibaba Cloud (Apache-2.0), run with the\n'
        'qwen-tts package (Apache-2.0) on PyTorch (BSD-3-Clause). Repacked for Voxa.\n')
    print(f'{out}: Voxa Studio, {len(VOICES)} voices, command {" ".join(command)}')


if __name__ == '__main__':
    a = sys.argv[1:]
    link = '--link-env' in a
    a = [x for x in a if x != '--link-env']
    env = None
    if '--env' in a:
        i = a.index('--env')
        env = Path(a[i + 1])
        del a[i:i + 2]
    if len(a) != 2:
        sys.exit(__doc__)
    main(Path(a[0]), Path(a[1]), env, link)
