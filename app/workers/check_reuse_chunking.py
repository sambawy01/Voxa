"""Checks reuse_worker.py's chunking and overlap-add on CPU, without the network or a GPU.

    python3 -I workers/check_reuse_chunking.py <app/workers> <re-use-snapshot>

Uses NVIDIA's own STFT code from the RE-USE repository with an identity model in place of
the network, so the output must equal the input (to float precision) at exactly the same
length, for long and short clips at several sample rates. Needs torch, numpy and pyyaml.
"""
import sys, numpy as np, torch
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2])
import reuse_worker as rw
from models.stfts import mag_phase_stft, mag_phase_istft
from utils.util import load_config, pad_or_trim_to_match
e = rw.ReuseEngine.__new__(rw.ReuseEngine)
e.torch, e.stft, e.istft, e.fit = torch, mag_phase_stft, mag_phase_istft, pad_or_trim_to_match
e.device = torch.device('cpu'); e.cfg = load_config(sys.argv[2] + '/' + rw.RECIPE)
e.model = lambda mag, pha: (mag, pha, None)   # identity in place of the network
e.chunk_seconds, e.hop = 5.0, 0.5
rng = np.random.default_rng(1)
for rate, secs in [(48000, 12.37), (16000, 3.1), (44100, 7.0), (22050, 2.0), (44100, 0.3)]:
    n = int(rate * secs)
    t = np.arange(n) / rate
    x = (0.3 * np.sin(2 * np.pi * 220 * t) * np.sin(2 * np.pi * 0.7 * t) + 0.01 * rng.standard_normal(n)).astype('float32')
    y = e.clean(x, rate)
    err = np.sqrt(np.mean((y - x) ** 2)) / np.sqrt(np.mean(x ** 2))
    print(f'{rate} Hz, {secs} s: length {len(x)} -> {len(y)}, relative error {err:.2e}')
    assert len(y) == len(x) and err < 1e-4
print('chunking ok')
