"""Build a Voxa Clean Film (NVIDIA RE-USE) clean-up worker package.

    python -I app/tools/build_reuse_package.py <re-use-snapshot> <out-dir> [--env <python-env>]

<re-use-snapshot> is a download of https://huggingface.co/nvidia/NVIDIA-NemotronLabs-RE-USE
(OpenMDW-1.1; keep its licence and notices). The pack runs app/workers/reuse_worker.py
with a Python that has PyTorch with CUDA and mamba-ssm, either the system `python3` or
an environment bundled with --env. RE-USE needs an NVIDIA GPU.

Then sign it with "engine": "worker" and "tier": "gpu" in the entry.
"""
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKERS = HERE.parent / 'workers'
NEEDED = ('config.json', 'model.safetensors', 'LICENSE', 'models', 'utils', 'recipes')
NOTICES = ('LICENSE', 'README.md', 'bias.md', 'explainability.md', 'privacy.md', 'safety_security.md')


def manifest(command: list) -> dict:
    return {'protocol': 1, 'kind': 'cleanup', 'family': 'Voxa Clean Film', 'command': command,
            'start_timeout_s': 600, 'request_timeout_s': 3600}


def main(src: Path, out: Path, env: Path = None) -> None:
    missing = [n for n in NEEDED if not (src / n).exists()]
    if missing:
        sys.exit(f'{src} is not a RE-USE snapshot (missing {", ".join(missing)})')
    if 'OpenMDW' not in (src / 'LICENSE').read_text(encoding='utf-8', errors='replace'):
        sys.exit(f'{src}/LICENSE is not the OpenMDW licence; check the model licence first')
    out.mkdir(parents=True, exist_ok=False)
    dest = out / 'reuse'
    dest.mkdir()
    for n in ('config.json', 'model.safetensors'):
        shutil.copy2(src / n, dest / n)
    for d in ('models', 'utils', 'recipes'):
        shutil.copytree(src / d, dest / d)
    for n in NOTICES:
        if (src / n).is_file():
            shutil.copy2(src / n, out / f'RE-USE-{n}')
    for f in ('voxa_worker.py', 'reuse_worker.py'):
        shutil.copy2(WORKERS / f, out / f)
    worker = ['reuse_worker.py', '--model', 'reuse']
    if env:
        shutil.copytree(env, out / 'env', symlinks=True)
        py = next((r for r in ('bin/python3', 'python.exe') if (out / 'env' / r).exists()), None)
        if not py:
            sys.exit(f'{env} has no python')
        command = [f'./env/{py}', '-I'] + worker
    else:
        command = ['python3', '-I'] + worker
    (out / 'worker.json').write_text(json.dumps(manifest(command), indent=2) + '\n')
    (out / 'SOURCE.txt').write_text(
        'NVIDIA NemotronLabs RE-USE (OpenMDW-1.1): speech enhancement for recorded dialogue.\n'
        'Licence and NVIDIA notices are kept as RE-USE-*. Repacked for Voxa.\n')
    print(f'{out}: Voxa Clean Film (RE-USE), command {" ".join(command)}')


if __name__ == '__main__':
    a = sys.argv[1:]
    env = None
    if '--env' in a:
        i = a.index('--env')
        env = Path(a[i + 1])
        del a[i:i + 2]
    if len(a) != 2:
        sys.exit(__doc__)
    main(Path(a[0]), Path(a[1]), env)
