"""The on-device half of the listening test: the engines Voxa Lite runs in the browser,
here run on a plain CPU with their Python packages (the same weights the browser gets).

    pip install supertonic pocket-tts soundfile numpy onnxruntime tokenizers
    python gpu/compare_device.py [out-dir]      # default: out/compare
    DEVICE_ENGINES=supertonic,hojo python gpu/compare_device.py   # a subset

Engines: Supertonic 3 (31 languages, no Chinese), Pocket TTS in its standard and HD (24-layer)
models for English, German, Spanish and French, Hojo TTS Light for English and Chinese when
HOJO_REPO points at a checkout of github.com/HojoAI/Hojo-TTS-Light, MOSS-TTS-Nano (100M, ONNX,
20 languages) when MOSS_NANO_REPO points at a checkout of github.com/OpenMOSS/MOSS-TTS-Nano, and
LFM3-Omni-2B (Liquid AI's speech-to-speech model, here in its text-to-speech mode; needs
transformers>=5.20.2, torch and torchvision). Clips and timings are added
to results.json beside the GPU engines' (gpu/compare.py), so one page compares them all.
Timings are CPU seconds on this machine, a rough guide to phone speed, not a measurement of it.
"""
import ast
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def load_texts():
    """The paragraphs from gpu/compare.py, read without importing Modal."""
    tree = ast.parse(open(os.path.join(HERE, 'compare.py'), encoding='utf-8').read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', None) == 'TEXTS':
            return ast.literal_eval(node.value)
    raise SystemExit('TEXTS not found in gpu/compare.py')


TEXTS = load_texts()
SUPERTONIC_LANGS = [lang for lang in TEXTS if lang != 'zh']  # Supertonic 3 has no Chinese
SUPERTONIC_VOICE = 'F1'
POCKET = {  # language -> (standard model, HD model, voice)
    'en': ('english_2026-10', 'english_2026-09_24l', 'alba'),
    'de': ('german', 'german_24l', 'juergen'),
    'es': ('spanish', 'spanish_24l', 'lola'),
    'fr': ('french', 'french_24l', 'estelle'),
}
HOJO = {'en': 'hojo_en_f_01', 'zh': 'hojo_zh_f_01'}
# MOSS-TTS-Nano clones the voice of a reference clip. Its bundled presets are clips of unknown
# origin (one is labelled as a politician), so this uses a CC0 donated voice from Kyutai's
# catalogue for every language, as Voxa would. Hindi is not among its 20 languages.
MOSS_NANO_LANGS = [lang for lang in TEXTS if lang != 'hi']
MOSS_REFERENCE = ('kyutai/tts-voices', 'voice-donations/0a67_enhanced.wav')
# LFM3-Omni speaks 19 languages and locales; Hindi and Russian are text-only. Names are those of
# its "Perform TTS in <language> using a <gender> voice." prompt.
LFM3_LANGS = {'en': 'American English', 'ar': 'Arabic', 'de': 'German', 'ja': 'Japanese', 'zh': 'Chinese',
              'es': 'Spanish', 'fr': 'French', 'ko': 'Korean'}


def to_mp3(samples, rate):
    import io
    import subprocess

    import numpy as np
    import soundfile as sf

    wav = io.BytesIO()
    sf.write(wav, np.asarray(samples, dtype='float32').reshape(-1), rate, format='WAV')
    return subprocess.run(['ffmpeg', '-loglevel', 'error', '-i', 'pipe:0', '-ac', '1', '-b:a', '64k', '-f', 'mp3', 'pipe:1'],
                          input=wav.getvalue(), capture_output=True, check=True).stdout


def timed(engine, name, lang, speak):
    row = {'engine': engine, 'engine_name': name, 'lang': lang, 'where': 'device'}
    try:
        start = time.perf_counter()
        samples, rate, voice = speak(TEXTS[lang])
        samples = __import__('numpy').asarray(samples, dtype='float32').reshape(-1)
        row['gen_seconds'] = round(time.perf_counter() - start, 2)
        row['audio_seconds'] = round(len(samples) / rate, 2)
        row['voice'] = voice
        row['mp3'] = to_mp3(samples, rate)
    except Exception as e:  # one language failing should not lose the others
        row['error'] = f'{type(e).__name__}: {e}'[:300]
    print(engine, lang, row.get('gen_seconds'), row.get('audio_seconds'), row.get('error', 'ok'), flush=True)
    return row


def supertonic_rows():
    from supertonic import TTS

    tts = TTS(auto_download=True)  # Supertonic 3 is the package default
    style = tts.get_voice_style(voice_name=SUPERTONIC_VOICE)
    rate = getattr(tts, 'sample_rate', 44100)
    tts.synthesize('Warm-up.', voice_style=style, lang='en')

    def speak_in(lang):
        def speak(text):
            wav, _ = tts.synthesize(text, voice_style=style, lang=lang)
            return wav, rate, SUPERTONIC_VOICE
        return speak
    return [timed('supertonic', 'Supertonic 3', lang, speak_in(lang)) for lang in SUPERTONIC_LANGS]


def pocket_rows():
    from pocket_tts import TTSModel

    rows = []
    for hd in (False, True):
        for lang, (standard, big, voice) in POCKET.items():
            engine, name = ('pocket_hd', 'Pocket TTS HD') if hd else ('pocket', 'Pocket TTS')
            try:
                model = TTSModel.load_model(language=big if hd else standard)
                state = model.get_state_for_audio_prompt(voice)
                model.generate_audio(state, 'Warm-up.')
            except Exception as e:
                rows.append({'engine': engine, 'engine_name': name, 'lang': lang, 'where': 'device',
                             'error': f'{type(e).__name__}: {e}'[:300]})
                continue

            def speak(text, model=model, state=state, voice=voice):
                return model.generate_audio(state, text).numpy(), model.sample_rate, voice
            rows.append(timed(engine, name, lang, speak))
    return rows


def hojo_rows():
    repo = os.environ.get('HOJO_REPO')
    if not repo:
        print('hojo: skipped (set HOJO_REPO to a Hojo-TTS-Light checkout)', flush=True)
        return []
    sys.path.insert(0, os.path.join(repo, 'Hojo-TTS-Light-40M'))
    from infer import get_model

    tts = get_model(repo_id='HojoAI/Hojo-TTS-Light-40M')
    tts.generate('Warm-up.', voice=HOJO['en'])
    # Hojo reads raw digits badly; the paragraphs spell numbers out already.
    return [timed('hojo', 'Hojo TTS Light 40M', lang,
                  lambda text, v=voice: (tts.generate(text, voice=v), tts.sample_rate, v))
            for lang, voice in HOJO.items()]


def sentences(text):
    """Whole sentences, so a long paragraph is spoken a piece at a time."""
    import re
    return [p.strip() for p in re.findall(r'[^.!?\u3002\uff01\uff1f]+[.!?\u3002\uff01\uff1f]*', text) if p.strip()]


def moss_nano_rows():
    repo = os.environ.get('MOSS_NANO_REPO')
    if not repo:
        print('moss_nano: skipped (set MOSS_NANO_REPO to a MOSS-TTS-Nano checkout)', flush=True)
        return []
    import numpy as np
    from huggingface_hub import hf_hub_download

    sys.path.insert(0, repo)
    from onnx_tts_runtime import OnnxTtsRuntime

    reference = hf_hub_download(MOSS_REFERENCE[0], MOSS_REFERENCE[1])
    runtime = OnnxTtsRuntime(model_dir=os.environ.get('MOSS_NANO_MODELS'), thread_count=4)  # fetches the ONNX files once
    output = os.path.join(os.environ.get('MOSS_NANO_OUT', '.'), 'moss_nano_tmp.wav')
    kwargs = dict(prompt_audio_path=reference, output_audio_path=output, streaming=True, seed=7)
    runtime.synthesize(text='Warm-up.', **kwargs)

    def speak(text):
        result = runtime.synthesize(text=text, **kwargs)
        wave = np.asarray(result['waveform'], dtype='float32')
        return (wave.mean(axis=1) if wave.ndim == 2 else wave), result['sample_rate'], 'CC0 donated voice (cloned)'
    return [timed('moss_nano', 'MOSS-TTS-Nano 100M', lang, speak) for lang in MOSS_NANO_LANGS]


def lfm3_rows():
    import numpy as np
    import torch
    from transformers import AutoModelForMultimodalLM, AutoProcessor

    model_id = 'LiquidAI/LFM3-Omni-2B'
    model = AutoModelForMultimodalLM.from_pretrained(model_id, dtype=torch.float32, device_map='cpu',
                                                     trust_remote_code=True).eval()
    processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
    codec = processor.audio_tokenizer.to(device='cpu').eval()

    def say(text, language):
        conversation = [{'role': 'system', 'content': f'Perform TTS in {language} using a female voice.'},
                        {'role': 'user', 'content': text}]
        inputs = processor.apply_chat_template(conversation, add_generation_prompt=True, tokenize=True,
                                               return_dict=True, return_tensors='pt')
        with torch.no_grad():
            out = model.generate(**inputs, max_new_tokens=768, generation_mode='audio',
                                 audio_temperature=0.4, audio_repetition_penalty=1.2)
            wave = codec.decode(out.audio_codes[:1, :, :out.audio_lengths[0]]).audio_values[0]
        return wave.squeeze(0).float().cpu().numpy()

    say('Warm-up.', 'American English')

    def speak_in(lang):
        def speak(text):
            gap = np.zeros(int(24000 * 0.2), dtype='float32')
            parts = []
            for piece in sentences(text):
                if parts:
                    parts.append(gap)
                parts.append(say(piece, LFM3_LANGS[lang]))
            return np.concatenate(parts), 24000, 'female voice (prompted)'
        return speak
    return [timed('lfm3', 'LFM3-Omni 2B (speech mode)', lang, speak_in(lang)) for lang in LFM3_LANGS]


def main(out):
    os.makedirs(out, exist_ok=True)
    rows = []
    only = {e for e in os.environ.get('DEVICE_ENGINES', '').split(',') if e}
    for make in (supertonic_rows, pocket_rows, hojo_rows, moss_nano_rows, lfm3_rows):
        if only and make.__name__.replace('_rows', '') not in only:
            continue
        try:
            rows += make()
        except Exception as e:  # an engine that will not load still gets a line
            print(make.__name__, 'failed:', e, flush=True)
            rows.append({'engine': make.__name__.replace('_rows', ''), 'where': 'device',
                         'error': f'{type(e).__name__}: {e}'[:300]})
    for row in rows:
        mp3 = row.pop('mp3', None)
        if mp3:
            row['file'] = f"{row['engine']}-{row['lang']}.mp3"
            with open(os.path.join(out, row['file']), 'wb') as f:
                f.write(mp3)
    path = os.path.join(out, 'results.json')
    results = json.load(open(path, encoding='utf-8')) if os.path.exists(path) else {'texts': TEXTS, 'rows': []}
    ran = {r['engine'] for r in rows}
    results['rows'] = [r for r in results['rows'] if r.get('where') != 'device' or r['engine'] not in ran] + rows
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print(f'{sum("file" in r for r in rows)} on-device clips in {out}')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'out/compare')
