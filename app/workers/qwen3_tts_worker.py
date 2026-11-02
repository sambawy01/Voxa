"""Voxa Studio: Qwen3-TTS (Apache-2.0) as a Voxa model worker (roadmap M11c).

    python3 -I qwen3_tts_worker.py [--model model] [--device auto|cuda|mps|cpu]

Runs Qwen3-TTS-12Hz CustomVoice (0.6B or 1.7B) with the `qwen-tts` package on PyTorch:
CUDA on NVIDIA, MPS on Apple Silicon, or CPU (slow; about 3x slower than real time for
0.6B on a laptop CPU in Voxa's check). See voxa_worker.py for the protocol.

Each speaker is offered in its native language, as the model card recommends. Speed is
applied with a time stretch after generation, since the model has no speed control.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import voxa_worker  # noqa: E402

# Voxa voice id -> (Qwen speaker, Qwen language). The model card lists each speaker's
# native language; voices.json in the pack carries the display names.
VOICES = {
    'studio-ryan': ('ryan', 'english'),
    'studio-aiden': ('aiden', 'english'),
    'studio-vivian': ('vivian', 'chinese'),
    'studio-serena': ('serena', 'chinese'),
    'studio-uncle-fu': ('uncle_fu', 'chinese'),
    'studio-dylan': ('dylan', 'chinese'),
    'studio-eric': ('eric', 'chinese'),
    'studio-ono-anna': ('ono_anna', 'japanese'),
    'studio-sohee': ('sohee', 'korean'),
}


def pick_device(want: str):
    import torch
    if want != 'auto':
        return want
    if torch.cuda.is_available():
        return 'cuda:0'
    if getattr(torch.backends, 'mps', None) and torch.backends.mps.is_available():
        return 'mps'
    return 'cpu'


class QwenEngine:
    def __init__(self, model_dir: str, device: str):
        import torch
        from qwen_tts import Qwen3TTSModel

        self.device = pick_device(device)
        dtype = torch.bfloat16 if self.device.startswith('cuda') else torch.float32
        kwargs = {'device_map': self.device, 'dtype': dtype}
        if self.device.startswith('cuda'):
            try:
                import flash_attn  # noqa: F401
                kwargs['attn_implementation'] = 'flash_attention_2'
            except ImportError:
                pass
        print(f'qwen3-tts: loading {model_dir} on {self.device}', file=sys.stderr, flush=True)
        self.tts = Qwen3TTSModel.from_pretrained(model_dir, **kwargs)
        self.sample_rate = int(self.tts.model.speech_tokenizer.get_output_sample_rate())
        speakers = set(self.tts.model.get_supported_speakers())
        self.voices = [v for v, (spk, _) in VOICES.items() if spk in speakers]

    def synthesize(self, text, voice, speed):
        speaker, language = VOICES[voice]
        wavs, sr = self.tts.generate_custom_voice(text=text, language=language, speaker=speaker)
        audio = wavs[0]
        if sr != self.sample_rate:
            raise RuntimeError(f'model returned {sr} Hz, expected {self.sample_rate}')
        if abs(speed - 1.0) > 0.01:
            import librosa
            audio = librosa.effects.time_stretch(audio.astype('float32'), rate=speed)
        return audio.clip(-1.0, 1.0)


def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


if __name__ == '__main__':
    here = os.path.dirname(os.path.abspath(__file__))
    model = arg('--model', os.path.join(here, 'model'))
    voxa_worker.serve(QwenEngine(model, arg('--device', 'auto')))
