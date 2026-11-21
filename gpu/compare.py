"""A listening test: the same paragraph in ten languages from each candidate engine.

    modal run gpu/compare.py                 # writes out/compare/*.mp3 and results.json

Candidates are the multilingual models whose weights allow commercial use:
Chatterbox Multilingual (MIT), Qwen3-TTS and Audio8-TTS-Realistic (Apache 2.0), with Kokoro, Voxa's current
engine, as the baseline where it has the language. Each engine runs in its own image on an
L4, because their pinned torch and transformers versions do not agree. One warm-up line is
spoken first so the timings leave out loading and compiling.
"""
import io
import json
import os
import time

import modal

GPU = 'L4'

TEXTS = {
    'en': 'Every morning, the old bookshop on the corner opens its doors at nine. The owner makes a cup of tea, '
          'sorts the new arrivals, and reads the first page of each one aloud, just to hear how it sounds.',
    'ar': 'كل صباح، تفتح المكتبة القديمة في زاوية الشارع أبوابها في التاسعة. تُعِدّ صاحبتها كوبًا من الشاي، '
          'وترتّب الكتب الجديدة، ثم تقرأ الصفحة الأولى من كل كتاب بصوت عالٍ، فقط لتسمع كيف يبدو.',
    'de': 'Jeden Morgen öffnet die alte Buchhandlung an der Ecke um neun Uhr ihre Türen. Die Besitzerin kocht sich '
          'eine Tasse Tee, sortiert die Neuzugänge und liest von jedem Buch die erste Seite laut vor, nur um zu hören, '
          'wie sie klingt.',
    'ja': '毎朝、角にある古い本屋は九時に店を開けます。店主はお茶を一杯いれて、新しく届いた本を並べ、'
          'どの本も最初のページを声に出して読みます。どんな響きがするのか、聞いてみたいからです。',
    'zh': '每天早上九点，街角那家老书店准时开门。店主先给自己泡一杯茶，整理新到的书，'
          '然后把每本书的第一页大声读出来，只是想听听它听起来是什么样子。',
    'es': 'Cada mañana, la vieja librería de la esquina abre sus puertas a las nueve. La dueña se prepara una taza '
          'de té, ordena los libros nuevos y lee en voz alta la primera página de cada uno, solo para oír cómo suena.',
    'fr': 'Chaque matin, la vieille librairie du coin ouvre ses portes à neuf heures. La propriétaire se prépare '
          'une tasse de thé, trie les nouveautés et lit à voix haute la première page de chacune, juste pour '
          'entendre comment elle sonne.',
    'hi': 'हर सुबह, कोने की पुरानी किताबों की दुकान नौ बजे अपने दरवाज़े खोलती है। मालकिन एक कप चाय बनाती हैं, '
          'नई आई किताबें सजाती हैं, और हर किताब का पहला पन्ना ज़ोर से पढ़ती हैं, बस यह सुनने के लिए कि वह कैसा लगता है।',
    'ko': '매일 아침, 모퉁이의 오래된 서점은 아홉 시에 문을 엽니다. 주인은 차 한 잔을 우리고, 새로 들어온 책들을 '
          '정리한 뒤, 책마다 첫 페이지를 소리 내어 읽습니다. 어떤 소리가 나는지 듣고 싶어서요.',
    'ru': 'Каждое утро старый книжный магазин на углу открывает свои двери в девять. Хозяйка заваривает себе '
          'чашку чая, разбирает новые поступления и читает вслух первую страницу каждой книги, просто чтобы '
          'услышать, как она звучит.',
}

# Qwen3-TTS takes language names and has nine named speakers; any of them can speak any of
# its languages, but a native speaker sounds best where there is one.
QWEN_LANGS = {'en': 'English', 'de': 'German', 'ja': 'Japanese', 'zh': 'Chinese', 'es': 'Spanish',
              'fr': 'French', 'ko': 'Korean', 'ru': 'Russian'}
QWEN_SPEAKERS = {'zh': 'Vivian', 'ja': 'Ono_Anna', 'ko': 'Sohee'}
QWEN_DEFAULT_SPEAKER = 'Ryan'
# Audio8 has no named voices: without a reference it picks a new speaker every time. So one
# English line is spoken first, with a fixed seed, and every language is then cloned from it,
# which also shows whether a cloned voice holds up in other languages. It reads the language
# from the text, so every one of the ten is tried.
AUDIO8_REPO = 'Edge0/Audio8-TTS-Realistic-0.6b'
AUDIO8_REVISION = '1e857568bfd4f02931aca3988ee773a44d2d26d3'  # the custom model code runs, so pin what was reviewed
AUDIO8_DIR = '/model/audio8'
AUDIO8_REFERENCE = 'Good evening, and thank you for joining me. Tonight I would like to read you something short.'
KOKORO_VOICES = {'en': 'af_heart', 'ja': 'jf_alpha', 'zh': 'zf_xiaobei', 'es': 'ef_dora',
                 'fr': 'ff_siwis', 'hi': 'hf_alpha'}


def to_mp3(samples, rate):
    """Encode float samples as a 64 kbps mono MP3, small enough to embed in a web page."""
    import subprocess

    import numpy as np
    import soundfile as sf

    wav = io.BytesIO()
    sf.write(wav, np.asarray(samples, dtype='float32').reshape(-1), rate, format='WAV')
    return subprocess.run(['ffmpeg', '-loglevel', 'error', '-i', 'pipe:0', '-ac', '1', '-b:a', '64k', '-f', 'mp3', 'pipe:1'],
                          input=wav.getvalue(), capture_output=True, check=True).stdout


def run_all(engine, langs, speak):
    """Speak every language with `speak(text, lang) -> (samples, rate, voice)`, timing each."""
    speak(TEXTS['en'][:60], 'en' if 'en' in langs else langs[0])  # warm-up
    results = []
    for lang in langs:
        row = {'engine': engine, 'lang': lang}
        try:
            start = time.perf_counter()
            samples, rate, voice = speak(TEXTS[lang], lang)
            row['gen_seconds'] = round(time.perf_counter() - start, 2)
            row['audio_seconds'] = round(len(samples) / rate, 2)
            row['voice'] = voice
            row['mp3'] = to_mp3(samples, rate)
        except Exception as e:  # one language failing should not lose the others
            row['error'] = f'{type(e).__name__}: {e}'[:300]
        print(engine, lang, row.get('gen_seconds'), row.get('audio_seconds'), row.get('error', 'ok'), flush=True)
        results.append(row)
    return results


base = modal.Image.debian_slim(python_version='3.11').apt_install('ffmpeg', 'libsndfile1', 'git')

chatterbox_image = base.pip_install('chatterbox-tts', 'soundfile', 'numpy').env({'HF_HOME': '/model'})
qwen_image = base.pip_install('qwen-tts', 'soundfile', 'numpy').env({'HF_HOME': '/model'})
# Versions the model card was tested with. The weights are downloaded into the image because
# the model code fetches its codec from the repo's latest files, not the pinned revision.
audio8_image = (
    base.pip_install('torch==2.8.0', 'torchaudio==2.8.0', 'transformers==4.57.3', 'safetensors',
                     'huggingface_hub', 'soundfile', 'numpy')
    .run_commands(f"python -c \"from huggingface_hub import snapshot_download; "
                  f"snapshot_download('{AUDIO8_REPO}', revision='{AUDIO8_REVISION}', local_dir='{AUDIO8_DIR}')\"")
    .env({'HF_HUB_OFFLINE': '1'})
)
kokoro_image = (
    base.apt_install('espeak-ng', 'build-essential', 'cmake')
    .pip_install('torch', 'kokoro>=0.9.4', 'soundfile', 'numpy', 'misaki[zh]>=0.9.4',
                 'fugashi', 'jaconv', 'mojimoji', 'pyopenjtalk', 'unidic-lite')
    .env({'HF_HOME': '/model'})
)

app = modal.App('voxa-compare')


@app.function(image=chatterbox_image, gpu=GPU, timeout=1800)
def chatterbox():
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS

    try:
        model = ChatterboxMultilingualTTS.from_pretrained(device='cuda', t3_model='v3')
        name = 'Chatterbox Multilingual V3'
    except TypeError:  # an older release without the V3 switch
        model = ChatterboxMultilingualTTS.from_pretrained(device='cuda')
        name = 'Chatterbox Multilingual'

    def speak(text, lang):
        wav = model.generate(text, language_id=lang)
        return wav.squeeze().cpu().numpy(), model.sr, 'default'

    rows = run_all('chatterbox', list(TEXTS), speak)
    for row in rows:
        row['engine_name'] = name
    return rows


@app.function(image=qwen_image, gpu=GPU, timeout=1800)
def qwen():
    import torch
    from qwen_tts import Qwen3TTSModel

    model = Qwen3TTSModel.from_pretrained('Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice',
                                          device_map='cuda:0', dtype=torch.bfloat16)

    def speak(text, lang):
        speaker = QWEN_SPEAKERS.get(lang, QWEN_DEFAULT_SPEAKER)
        wavs, rate = model.generate_custom_voice(text=text, language=QWEN_LANGS[lang], speaker=speaker)
        return wavs[0], rate, speaker

    rows = run_all('qwen', list(QWEN_LANGS), speak)
    for row in rows:
        row['engine_name'] = 'Qwen3-TTS 1.7B'
    return rows


@app.function(image=audio8_image, gpu=GPU, timeout=1800)
def audio8():
    import torch
    from transformers import AutoModel, AutoProcessor

    processor = AutoProcessor.from_pretrained(AUDIO8_DIR, trust_remote_code=True)
    model = AutoModel.from_pretrained(AUDIO8_DIR, trust_remote_code=True, dtype=torch.bfloat16).eval().to('cuda')
    rate = model.config.codec_sample_rate

    def generate(text, reference=None):
        if reference is None:
            inputs = processor(text=[text], return_tensors='pt')
        else:
            inputs = processor(text=[text], reference_audio=[(reference, rate)],
                               reference_text=[AUDIO8_REFERENCE], return_tensors='pt')
        inputs = {k: v.to('cuda') for k, v in inputs.items()}
        with torch.inference_mode():
            out = model.generate(**inputs, max_new_tokens=1024, temperature=0.8, top_p=0.95, top_k=50,
                                 do_sample=True, return_dict_in_generate=True)
            waves, lengths = model.decode_audio(out.codes)
        # finished is False when it ran out of frames instead of ending: the clip is cut off
        return waves[0, :int(lengths[0])].float().cpu().numpy(), bool(out.finished[0])

    torch.manual_seed(0)
    reference, _ = generate(AUDIO8_REFERENCE)

    def speak(text, lang):
        samples, finished = generate(text, reference)
        if not finished:
            print('audio8', lang, 'cut off: no end of speech within 1024 frames', flush=True)
        return samples, rate, 'cloned from a generated English line'

    rows = run_all('audio8', list(TEXTS), speak)
    for row in rows:
        row['engine_name'] = 'Audio8-TTS-Realistic 0.6B'
    return rows


@app.function(image=kokoro_image, gpu=GPU, timeout=1800)
def kokoro():
    import numpy as np
    from kokoro import KModel, KPipeline

    model = KModel(repo_id='hexgrad/Kokoro-82M').to('cuda').eval()
    pipelines = {}

    def speak(text, lang):
        voice = KOKORO_VOICES[lang]
        code = voice[0]
        if code not in pipelines:
            pipelines[code] = KPipeline(lang_code=code, repo_id='hexgrad/Kokoro-82M', model=model)
        parts = [r.audio.cpu().numpy() for r in pipelines[code](text, voice=voice) if r.audio is not None]
        return np.concatenate(parts), 24000, voice

    rows = run_all('kokoro', list(KOKORO_VOICES), speak)
    for row in rows:
        row['engine_name'] = 'Kokoro 82M (current)'
    return rows


@app.local_entrypoint()
def main(out: str = 'out/compare'):
    os.makedirs(out, exist_ok=True)
    calls = {'chatterbox': chatterbox.spawn(), 'qwen': qwen.spawn(), 'audio8': audio8.spawn(),
             'kokoro': kokoro.spawn()}
    rows = []
    for engine, call in calls.items():
        try:
            rows += call.get()
        except Exception as e:  # an engine that will not even load still gets a line in the results
            print(engine, 'failed:', e, flush=True)
            rows.append({'engine': engine, 'error': f'{type(e).__name__}: {e}'[:300]})
    for row in rows:
        mp3 = row.pop('mp3', None)
        if mp3:
            row['file'] = f"{row['engine']}-{row['lang']}.mp3"
            with open(os.path.join(out, row['file']), 'wb') as f:
                f.write(mp3)
    with open(os.path.join(out, 'results.json'), 'w') as f:
        json.dump({'gpu': GPU, 'texts': TEXTS, 'rows': rows}, f, ensure_ascii=False, indent=1)
    print(f'{sum("file" in r for r in rows)} clips in {out}')
