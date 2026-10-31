"""A listening test: the same paragraph in ten languages from each candidate engine.

    modal run gpu/compare.py                 # writes out/compare/*.mp3 and results.json

Candidates are the models whose weights allow commercial use: VoxCPM2 (Apache 2.0),
Chatterbox Multilingual (MIT) and Qwen3-TTS (Apache 2.0), Dia2 (Apache 2.0) for two-host
podcasts, and an Egyptian Arabic fine-tune of Chatterbox, with Kokoro, Voxa's current engine,
as the baseline where it has the language, plus three newer models: Audio8-TTS-Realistic
(Apache 2.0, 0.6B), MOSS-TTS-v1.5 (Apache 2.0, 8.5B, 31 languages) and NVIDIA Magpie TTS
Multilingual (NVIDIA Open Model License, 0.36B). gpu/compare_device.py adds the on-device engines.

    modal run gpu/compare.py --engines voxcpm,dia2   # a subset Each engine runs in its own image on an
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

# Texts for one engine each: Egyptian colloquial Arabic, and a two-host podcast exchange.
EXTRA_TEXTS = {
    'ar-eg': 'كل يوم الصبح، المكتبة القديمة اللي على الناصية بتفتح بابها الساعة تسعة. صاحبتها بتعمل لنفسها '
             'كوباية شاي، وبترتب الكتب الجديدة، وبعدين بتقرا أول صفحة من كل كتاب بصوت عالي، بس علشان تسمع شكلها عامل إزاي.',
    'en-podcast': '[S1] So every morning she opens the shop at nine, makes a cup of tea, and reads the first page '
                  'of every new book out loud. [S2] Out loud? To nobody? [S1] To nobody! She says she wants to hear '
                  'how it sounds. [S2] (laughs) Honestly, that might be the best quality check I have ever heard of.',
}
ALL_TEXTS = {**TEXTS, **EXTRA_TEXTS}
VOXCPM_VOICE = '(A warm, clear adult female narrator, calm and natural)'

# MOSS-TTS-v1.5 takes language names. Magpie takes codes, has no Russian, and its voices are
# Aria 0, Jason 1, John 2, Leo 3, Sofia 4; it speaks up to 20 seconds at a time, so text is
# sent a sentence at a time.
MOSS_NAMES = {'en': 'English', 'ar': 'Arabic', 'de': 'German', 'ja': 'Japanese', 'zh': 'Chinese', 'es': 'Spanish',
              'fr': 'French', 'hi': 'Hindi', 'ko': 'Korean', 'ru': 'Russian'}
MAGPIE_LANGS = [lang for lang in TEXTS if lang != 'ru']
MAGPIE_SPEAKER = 4


def sentences(text):
    import re
    return [p.strip() for p in re.findall(r'[^.!?\u3002\uff01\uff1f\u0964]+[.!?\u3002\uff01\uff1f\u0964]*', text) if p.strip()]


# Qwen3-TTS takes language names and has nine named speakers; any of them can speak any of
# its languages, but a native speaker sounds best where there is one.
QWEN_LANGS = {'en': 'English', 'de': 'German', 'ja': 'Japanese', 'zh': 'Chinese', 'es': 'Spanish',
              'fr': 'French', 'ko': 'Korean', 'ru': 'Russian'}
QWEN_SPEAKERS = {'zh': 'Vivian', 'ja': 'Ono_Anna', 'ko': 'Sohee'}
QWEN_DEFAULT_SPEAKER = 'Ryan'
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
    speak(ALL_TEXTS[langs[0]][:60], langs[0])  # warm-up
    results = []
    for lang in langs:
        row = {'engine': engine, 'lang': lang, 'where': 'gpu'}
        try:
            start = time.perf_counter()
            samples, rate, voice = speak(ALL_TEXTS[lang], lang)
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
kokoro_image = (
    base.apt_install('espeak-ng', 'build-essential', 'cmake')
    .pip_install('torch', 'kokoro>=0.9.4', 'soundfile', 'numpy', 'misaki[zh]>=0.9.4',
                 'fugashi', 'jaconv', 'mojimoji', 'pyopenjtalk', 'unidic-lite')
    .env({'HF_HOME': '/model'})
)

voxcpm_image = base.pip_install('voxcpm', 'soundfile', 'numpy').env({'HF_HOME': '/model'})
dia2_image = base.pip_install('git+https://github.com/nari-labs/dia2', 'soundfile', 'numpy').env({'HF_HOME': '/model'})

audio8_image = base.pip_install('torch>=2.5.0', 'torchaudio>=2.5.0', 'transformers>=4.57.0,<5', 'soundfile',
                                'safetensors', 'numpy').env({'HF_HOME': '/model'})
moss_image = base.pip_install('git+https://github.com/OpenMOSS/MOSS-TTS', 'soundfile', 'numpy',
                              extra_index_url='https://download.pytorch.org/whl/cu128').env({'HF_HOME': '/model'})
magpie_image = base.apt_install('libsndfile1').pip_install(
    'nemo_toolkit[tts] @ git+https://github.com/NVIDIA-NeMo/Speech.git@main', 'kaldialign', 'soundfile', 'numpy',
).env({'HF_HOME': '/model'})

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


@app.function(image=voxcpm_image, gpu=GPU, timeout=1800)
def voxcpm():
    from voxcpm import VoxCPM

    model = VoxCPM.from_pretrained('openbmb/VoxCPM2', load_denoiser=False)

    def speak(text, lang):
        # A voice designed from a description, the same in every language.
        wav = model.generate(text=VOXCPM_VOICE + text, cfg_value=2.0, inference_timesteps=10)
        return wav, model.tts_model.sample_rate, 'designed: warm female narrator'

    rows = run_all('voxcpm', list(TEXTS), speak)
    for row in rows:
        row['engine_name'] = 'VoxCPM2'
    return rows


@app.function(image=dia2_image, gpu=GPU, timeout=1800)
def dia2():
    import tempfile

    import soundfile as sf
    from dia2 import Dia2, GenerationConfig, SamplingConfig

    model = Dia2.from_repo('nari-labs/Dia2-2B', device='cuda', dtype='bfloat16')
    config = GenerationConfig(cfg_scale=2.0, audio=SamplingConfig(temperature=0.8, top_k=50))

    def speak(text, lang):
        with tempfile.NamedTemporaryFile(suffix='.wav') as f:
            model.generate(text, config=config, output_wav=f.name)
            wav, rate = sf.read(f.name, dtype='float32')
        return wav, rate, 'two hosts (S1, S2)'

    rows = run_all('dia2', ['en-podcast'], speak)
    for row in rows:
        row['engine_name'] = 'Dia2 2B (podcasts)'
    return rows


@app.function(image=chatterbox_image, gpu=GPU, timeout=1800)
def chatterbox_eg():
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS
    from huggingface_hub import hf_hub_download
    from safetensors.torch import load_file

    model = ChatterboxMultilingualTTS.from_pretrained(device='cuda')
    weights = hf_hub_download('AliAbdallah/egyptian-arabic-tts-chatterbox', 'model.safetensors')
    model.t3.load_state_dict(load_file(weights, device='cuda'), strict=False)
    model.t3.eval()

    def speak(text, lang):
        wav = model.generate(text, language_id='ar', exaggeration=0.5, cfg_weight=0.5, temperature=0.8)
        return wav.squeeze().cpu().numpy(), model.sr, 'single speaker (fine-tune)'

    rows = run_all('chatterbox_eg', ['ar-eg'], speak)
    for row in rows:
        row['engine_name'] = 'Chatterbox Egyptian Arabic (licence check pending)'
    return rows


@app.function(image=audio8_image, gpu=GPU, timeout=1800)
def audio8():
    import torch
    from transformers import AutoModel, AutoProcessor

    model_id = 'Edge0/Audio8-TTS-Realistic-0.6b'
    processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
    model = AutoModel.from_pretrained(model_id, trust_remote_code=True, dtype=torch.bfloat16).eval().to('cuda')

    def speak(text, lang):
        # No reference clip, so the model picks the voice. Its context is short, so long text goes in pieces.
        import numpy as np
        parts = []
        for piece in sentences(text):
            inputs = processor(text=[piece], return_tensors='pt')
            inputs = {k: v.to('cuda') for k, v in inputs.items()}
            with torch.inference_mode():
                out = model.generate(**inputs, max_new_tokens=1024, temperature=0.8, top_p=0.95, top_k=50,
                                     do_sample=True, return_dict_in_generate=True)
                waves, lengths = model.decode_audio(out.codes)
            parts.append(waves[0, :int(lengths[0])].float().cpu().numpy())
            parts.append(np.zeros(int(model.config.codec_sample_rate * 0.2), dtype='float32'))
        return np.concatenate(parts[:-1]), model.config.codec_sample_rate, 'no reference (model-chosen voice)'

    rows = run_all('audio8', list(TEXTS), speak)
    for row in rows:
        row['engine_name'] = 'Audio8-TTS-Realistic 0.6B'
    return rows


@app.function(image=moss_image, gpu='L40S', timeout=2400)  # 8.5B parameters: more memory than an L4
def moss15():
    import torch
    from transformers import AutoModel, AutoProcessor

    path = 'OpenMOSS-Team/MOSS-TTS-v1.5'
    processor = AutoProcessor.from_pretrained(path, trust_remote_code=True)
    processor.audio_tokenizer = processor.audio_tokenizer.to('cuda')
    model = AutoModel.from_pretrained(path, trust_remote_code=True, attn_implementation='sdpa',
                                      torch_dtype=torch.bfloat16).to('cuda').eval()

    def speak(text, lang):
        batch = processor([[processor.build_user_message(text=text, language=MOSS_NAMES[lang])]], mode='generation')
        with torch.no_grad():
            out = model.generate(input_ids=batch['input_ids'].to('cuda'),
                                 attention_mask=batch['attention_mask'].to('cuda'), max_new_tokens=4096)
        message = next(iter(processor.decode(out)))
        return message.audio_codes_list[0].float().cpu().numpy(), processor.model_config.sampling_rate, 'no reference (model-chosen voice)'

    rows = run_all('moss15', list(MOSS_NAMES), speak)
    for row in rows:
        row['engine_name'] = 'MOSS-TTS-v1.5 8.5B'
    return rows


@app.function(image=magpie_image, gpu=GPU, timeout=1800)
def magpie():
    import numpy as np
    from nemo.collections.tts.models import MagpieTTSModel

    model = MagpieTTSModel.from_pretrained('nvidia/magpie_tts_multilingual_357m')

    def speak(text, lang):
        parts = []
        for piece in sentences(text):
            audio, length = model.do_tts(piece, language=lang, apply_TN=True, speaker_index=MAGPIE_SPEAKER)
            parts.append(audio.squeeze().float().cpu().numpy()[:int(length)])
            parts.append(np.zeros(int(22050 * 0.2), dtype='float32'))
        return np.concatenate(parts[:-1]), 22050, 'Sofia (built-in)'

    rows = run_all('magpie', MAGPIE_LANGS, speak)
    for row in rows:
        row['engine_name'] = 'NVIDIA Magpie TTS 357M'
    return rows


ENGINES = {'voxcpm': voxcpm, 'audio8': audio8, 'moss15': moss15, 'magpie': magpie, 'chatterbox': chatterbox,
           'qwen': qwen, 'dia2': dia2, 'chatterbox_eg': chatterbox_eg, 'kokoro': kokoro}


@app.local_entrypoint()
def main(out: str = 'out/compare', engines: str = ','.join(ENGINES)):
    os.makedirs(out, exist_ok=True)
    chosen = [e.strip() for e in engines.split(',') if e.strip()]
    calls = {name: ENGINES[name].spawn() for name in chosen}
    rows = []
    for engine, call in calls.items():
        try:
            rows += call.get()
        except Exception as e:  # an engine that will not even load still gets a line in the results
            print(engine, 'failed:', e, flush=True)
            rows.append({'engine': engine, 'where': 'gpu', 'error': f'{type(e).__name__}: {e}'[:300]})
    for row in rows:
        mp3 = row.pop('mp3', None)
        if mp3:
            row['file'] = f"{row['engine']}-{row['lang']}.mp3"
            with open(os.path.join(out, row['file']), 'wb') as f:
                f.write(mp3)
    # Keep the other engines' rows (the on-device half, or engines run separately).
    path = os.path.join(out, 'results.json')
    results = json.load(open(path, encoding='utf-8')) if os.path.exists(path) else {'rows': []}
    results.update(gpu=GPU, texts=ALL_TEXTS)
    results['rows'] = [r for r in results['rows'] if r.get('engine') not in calls] + rows
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print(f'{sum("file" in r for r in rows)} clips in {out}')
