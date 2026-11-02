"""A reference worker for Voxa's model-worker protocol: no model and no dependencies.
It "speaks" one short tone per word (a different pitch per voice), so the app's worker
client can be tested on any machine. See voxa_worker.py for the protocol.

    python3 -I reference_worker.py [--voices a,b] [--fail-after N] [--crash-after N]

--voices names its voices (default: low,high); each one is a step higher in pitch.

--fail-after N reports an error from the Nth synthesis on; --crash-after N exits during
the Nth synthesis (tests restart and error handling).
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import voxa_worker  # noqa: E402


class ToneEngine:
    sample_rate = 16000

    def __init__(self, fail_after=None, crash_after=None, voices=('low', 'high')):
        self.voices = list(voices)
        self.count = 0
        self.fail_after = fail_after
        self.crash_after = crash_after

    def synthesize(self, text, voice, speed):
        self.count += 1
        if self.crash_after is not None and self.count >= self.crash_after:
            os._exit(3)
        if self.fail_after is not None and self.count >= self.fail_after:
            raise RuntimeError('out of GPU memory (simulated)')
        f = 220.0 * (2 ** self.voices.index(voice))
        n = int(self.sample_rate * 0.12 / max(speed, 0.1))
        out = []
        for _ in text.split():
            out.extend(0.3 * math.sin(2 * math.pi * f * i / self.sample_rate) for i in range(n))
            out.extend([0.0] * (n // 3))
        return out


def arg(name):
    if name in sys.argv:
        return sys.argv[sys.argv.index(name) + 1]
    return None


if __name__ == '__main__':
    n = lambda v: int(v) if v is not None else None  # noqa: E731
    voices = (arg('--voices') or 'low,high').split(',')
    voxa_worker.serve(ToneEngine(n(arg('--fail-after')), n(arg('--crash-after')), voices))
