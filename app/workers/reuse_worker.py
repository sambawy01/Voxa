"""Voxa Clean Film: NVIDIA RE-USE (OpenMDW-1.1) as a Voxa clean-up worker (roadmap M11d).

    python3 -I reuse_worker.py [--model reuse] [--chunk-seconds 5] [--hop 0.5]

Removes noise and reverb from recorded dialogue at its own sample rate (8-48 kHz), keeping
the exact length. It follows NVIDIA's inference_chunk.py from the model repository:
chunks of 5 s with 50% overlap, a Hann window and overlap-add, and the same STFT scaling
and "sweep artifact" fix. One change: each chunk is padded to a multiple of the STFT hop,
so chunk ends aren't lost at rates such as 44.1 kHz (see clean()).

**It needs an NVIDIA GPU** (CUDA): the model uses mamba-ssm kernels, and NVIDIA's own
script refuses CPU mode. It has not been run in Voxa's CI; see the roadmap.
`--model` is a snapshot of https://huggingface.co/nvidia/NVIDIA-NemotronLabs-RE-USE (its
models/, utils/, recipes/, config.json and model.safetensors).
"""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import voxa_worker  # noqa: E402

RECIPE = 'recipes/USEMamba_30x1_lr_00002_norm_05_vq_065_nfft_320_hop_40_NRIR_012_pha_0005_com_04_early_001.yaml'


def make_even(value):
    value = int(round(value))
    return value if value % 2 == 0 else value + 1


class ReuseEngine:
    def __init__(self, model_dir, chunk_seconds=5.0, hop=0.5):
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError('RE-USE needs an NVIDIA GPU (CUDA); none was found')
        sys.path.insert(0, model_dir)  # the repository's models/ and utils/ packages
        from models.generator_SEMamba_time_d4 import SEMamba
        from models.stfts import mag_phase_stft, mag_phase_istft
        from utils.util import load_config, pad_or_trim_to_match

        self.torch = torch
        self.stft, self.istft, self.fit = mag_phase_stft, mag_phase_istft, pad_or_trim_to_match
        self.device = torch.device('cuda')
        self.cfg = load_config(os.path.join(model_dir, RECIPE))
        print(f're-use: loading {model_dir}', file=sys.stderr, flush=True)
        self.model = SEMamba.from_pretrained(model_dir, cfg=self.cfg).to(self.device).eval()
        self.chunk_seconds, self.hop = chunk_seconds, hop

    def clean(self, samples, sample_rate):
        torch = self.torch
        st = self.cfg['stft_cfg']
        base = st['sampling_rate']
        n_fft = make_even(st['n_fft'] * sample_rate // base)
        hop_size = make_even(st['hop_size'] * sample_rate // base)
        win_size = make_even(st['win_size'] * sample_rate // base)
        compress = self.cfg['model_cfg']['compress_factor']
        relu = torch.nn.ReLU()
        x = torch.as_tensor(samples, dtype=torch.float32, device=self.device).reshape(1, -1)
        n = x.shape[1]
        chunk = min(int(self.chunk_seconds * sample_rate), n)
        hop = max(1, int(self.hop * chunk))
        window = torch.clamp(torch.hann_window(chunk, device=self.device), min=1e-7)
        out = torch.zeros_like(x)
        wsum = torch.zeros_like(x)
        chunks = max(1, math.ceil((n - chunk) / hop) + 1)
        with torch.no_grad():
            for i in range(chunks):
                start = i * hop if i < chunks - 1 else n - chunk
                piece = x[:, start:start + chunk]
                # The inverse STFT returns a multiple of hop_size samples. NVIDIA's script
                # pads the shortfall with ~0 and still counts it in the window sum, which
                # dips the output at every chunk end when the rate isn't a multiple of 8 kHz
                # (44.1 kHz: -1.2% error with an identity model). Pad the input up to a
                # multiple of hop_size first and trim after, so every sample is real.
                extra = (-piece.shape[1]) % hop_size
                padded = torch.nn.functional.pad(piece, (0, extra))
                mag, pha, _ = self.stft(padded, n_fft=n_fft, hop_size=hop_size, win_size=win_size,
                                        compress_factor=compress, center=True, addeps=False)
                amp_g, pha_g, _ = self.model(mag, pha)
                # NVIDIA's fix for a "strange sweep artifact".
                m = torch.expm1(relu(amp_g))
                zero_portion = torch.sum(m == 0, 1) / m.shape[1]
                amp_g[:, :, (zero_portion > 0.5)[0]] = 0
                y = self.istft(amp_g, pha_g, n_fft, hop_size, win_size, compress)
                y = self.fit(padded, y, pad_value=1e-8)[:, :piece.shape[1]]
                out[:, start:start + chunk] += y * window[:y.shape[1]]
                wsum[:, start:start + chunk] += window[:y.shape[1]]
        return (out / wsum).reshape(-1).clamp(-1, 1).cpu().numpy()


def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


if __name__ == '__main__':
    voxa_worker.serve(ReuseEngine(arg('--model', os.path.join(HERE, 'reuse')),
                                  float(arg('--chunk-seconds', 5)), float(arg('--hop', 0.5))))
