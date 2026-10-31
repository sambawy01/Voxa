"""Read-back check for the listening test: transcribe every clip with Whisper and score it
against the paragraph that was spoken.

    pip install faster-whisper soundfile numpy        # and ffmpeg
    python gpu/readback.py out/compare

Adds `readback` (0-100, how much of the source text Whisper heard) to each row of
results.json. It measures whether the words came out right, not whether the voice sounds
good, so it finds skipped and garbled words and nothing else. Two limits: Whisper small is
weaker in some languages (it also writes Japanese in kana, which lowers the score of a good
clip), and Chinese is read with a simplified-characters hint so the model is not penalised
for Whisper's choice of script. Read scores of different languages with that in mind.
"""
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile

WHISPER_LANG = {'en': 'en', 'ar': 'ar', 'ar-eg': 'ar', 'de': 'de', 'ja': 'ja', 'zh': 'zh', 'es': 'es',
                'fr': 'fr', 'hi': 'hi', 'ko': 'ko', 'ru': 'ru', 'en-podcast': 'en'}


def squash(text):
    """Letters and digits only, lowercase, with speaker tags and sound cues removed."""
    text = re.sub(r'\[S\d\]|\([^)]*\)', ' ', text)
    return re.sub(r'[\W_]+', '', text.lower())


def main(folder):
    import numpy as np
    import soundfile as sf
    from faster_whisper import WhisperModel

    path = os.path.join(folder, 'results.json')
    results = json.load(open(path, encoding='utf-8'))
    model = WhisperModel('small', device='cpu', compute_type='int8')
    wav = os.path.join(tempfile.gettempdir(), 'readback.wav')
    for row in results['rows']:
        if 'file' not in row:
            continue
        subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', os.path.join(folder, row['file']),
                        '-ar', '16000', '-ac', '1', wav], check=True)
        audio, _ = sf.read(wav, dtype='float32')
        segments, _ = model.transcribe(
            np.asarray(audio), beam_size=1, language=WHISPER_LANG.get(row['lang']),
            initial_prompt='以下是普通话的简体中文句子。' if row['lang'] == 'zh' else None)
        heard = ''.join(s.text for s in segments)
        row['readback'] = round(100 * difflib.SequenceMatcher(None, squash(results['texts'][row['lang']]), squash(heard)).ratio())
        print(f"{row['engine']} {row['lang']}: {row['readback']}%", flush=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'out/compare')
