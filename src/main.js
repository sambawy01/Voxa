import { KokoroTTS } from 'kokoro-js';
import { LANGUAGES, VOICES, voicesFor, voiceGender } from './voices.js';
import './style.css';

const MODEL_ID = 'onnx-community/Kokoro-82M-v1.0-ONNX';

const state = {
  tts: null,
  loading: false,
  lang: 'a',
  voice: 'af_heart',
  speed: 1.0,
  audioUrl: null,
  wav: null,
};

const el = (sel) => document.querySelector(sel);

/* ───────────────────────────── markup ───────────────────────────── */
el('#app').innerHTML = `
  <div class="wrap">
    <header>
      <div class="brand">
        <span class="mark">◉</span>
        <h1>Voxa</h1>
      </div>
      <p class="tagline">Text to speech. Nine languages, 54 voices. Runs in your browser — your text never leaves this device.</p>
    </header>

    <div class="grid">
      <section class="card">
        <label for="text">Your script</label>
        <textarea id="text" placeholder="Type or paste what you want spoken…"></textarea>
        <div class="meta"><span id="count">0 characters</span><span id="est"></span></div>
        <div class="samples">
          <button class="chip" data-sample="en">English sample</button>
          <button class="chip" data-sample="fr">Exemple français</button>
          <button class="chip" data-sample="es">Ejemplo español</button>
          <button class="chip" data-sample="clear">Clear</button>
        </div>
      </section>

      <section class="card">
        <label for="lang">Language</label>
        <select id="lang"></select>
        <p class="hint" id="langnote"></p>

        <label for="voice">Voice</label>
        <select id="voice"></select>
        <p class="hint" id="voicenote"></p>

        <label for="speed">Pacing <span class="val" id="speedv">1.00×</span></label>
        <input type="range" id="speed" min="0.6" max="1.4" step="0.05" value="1" />
        <p class="hint">Slower reads more clearly; faster suits adverts and lists.</p>

        <button id="go" class="primary">Generate speech</button>
        <p class="status" id="status"></p>

        <audio id="player" controls></audio>
        <a id="download" class="download" download="voxa.wav" hidden>Download WAV</a>
      </section>
    </div>

    <div class="card note">
      <strong>About the model.</strong> Kokoro-82M, 82 million parameters, Apache 2.0.
      It is genuinely good in English and thin elsewhere — the grade next to each voice is
      the authors' own rating of its training data, so you can see which are worth using.
      There is no emotion dial: tone comes from the voice you choose, the pacing, and your punctuation.
    </div>
  </div>
`;

/* ───────────────────────────── populate ───────────────────────────── */
const langSel = el('#lang');
langSel.innerHTML = LANGUAGES.map(
  (l) => `<option value="${l.code}">${l.flag} ${l.label}</option>`
).join('');
langSel.value = state.lang;

function refreshVoices() {
  const list = voicesFor(state.lang);
  const voiceSel = el('#voice');
  voiceSel.innerHTML = list
    .map((id) => {
      const v = VOICES[id];
      const g = v.grade ? ` · grade ${v.grade}` : '';
      const gd = voiceGender(id);
      return `<option value="${id}">${id.replace('_', ' ')}${gd ? ' (' + gd + ')' : ''}${g}</option>`;
    })
    .join('');
  state.voice = list[0];
  voiceSel.value = state.voice;
  updateVoiceNote();
  const lang = LANGUAGES.find((l) => l.code === state.lang);
  const n = list.length;
  el('#langnote').textContent =
    n === 1
      ? `${lang.label}: one voice only — the model is thin here. Expect an accent.`
      : `${n} voices for ${lang.label}.`;
}

function updateVoiceNote() {
  const v = VOICES[state.voice];
  if (!v) return;
  el('#voicenote').textContent = v.grade
    ? `Grade ${v.grade} — ${v.traits}.`
    : `${v.traits}. Not graded by the authors.`;
}

refreshVoices();

langSel.addEventListener('change', () => {
  state.lang = langSel.value;
  refreshVoices();
});
el('#voice').addEventListener('change', (e) => {
  state.voice = e.target.value;
  updateVoiceNote();
});
el('#speed').addEventListener('input', (e) => {
  state.speed = parseFloat(e.target.value);
  el('#speedv').textContent = state.speed.toFixed(2) + '×';
});

const SAMPLES = {
  en: 'Voxa turns your words into speech without sending them anywhere. Everything happens on your own device.',
  fr: 'Bonjour. Ceci est Voxa, une voix de synthèse qui fonctionne entièrement dans votre navigateur.',
  es: 'Hola. Esto es Voxa, una voz sintética que funciona completamente en tu navegador.',
};

document.querySelectorAll('.chip').forEach((c) => {
  c.addEventListener('click', () => {
    const k = c.dataset.sample;
    if (k === 'clear') {
      el('#text').value = '';
    } else {
      el('#text').value = SAMPLES[k];
      if (k !== 'en') {
        langSel.value = k;
        state.lang = k;
        refreshVoices();
      }
    }
    updateCount();
  });
});

function updateCount() {
  const t = el('#text').value;
  el('#count').textContent = `${t.length} characters`;
  // ~14 characters per second of speech is a decent estimate
  el('#est').textContent = t.length ? `≈ ${Math.ceil(t.length / 14)}s of audio` : '';
}
el('#text').addEventListener('input', updateCount);
updateCount();

/* ───────────────────────────── load model ───────────────────────────── */
async function ensureModel() {
  if (state.tts) return state.tts;
  if (state.loading) throw new Error('still loading');
  state.loading = true;
  setStatus('Downloading the model (about 90 MB, cached after the first time)…', true);
  try {
    state.tts = await KokoroTTS.from_pretrained(MODEL_ID, {
      dtype: 'q8',
      device: 'wasm',
      progress_callback: (p) => {
        if (p && p.status === 'progress' && p.total) {
          const pct = Math.round((p.loaded / p.total) * 100);
          setStatus(`Downloading model… ${pct}%`, true);
        }
      },
    });
    return state.tts;
  } finally {
    state.loading = false;
  }
}

function setStatus(msg, busy = false) {
  const s = el('#status');
  s.textContent = msg;
  s.className = 'status' + (busy ? ' busy' : '');
}

/* ───────────────────────────── generate ───────────────────────────── */
el('#go').addEventListener('click', async () => {
  const text = el('#text').value.trim();
  if (!text) {
    setStatus('Type something first.');
    return;
  }
  const btn = el('#go');
  btn.disabled = true;
  el('#player').hidden = true;
  el('#download').hidden = true;
  const t0 = performance.now();

  try {
    const tts = await ensureModel();
    setStatus('Generating…', true);

    const audio = await tts.generate(text, {
      voice: state.voice,
      speed: state.speed,
    });

    if (state.audioUrl) URL.revokeObjectURL(state.audioUrl);
    state.audioUrl = URL.createObjectURL(audio.toBlob());
    const player = el('#player');
    player.src = state.audioUrl;
    player.hidden = false;
    player.play().catch(() => {});

    const dl = el('#download');
    dl.href = state.audioUrl;
    dl.hidden = false;

    const secs = ((performance.now() - t0) / 1000).toFixed(1);
    const dur = (audio.audio.length / audio.sampling_rate).toFixed(1);
    setStatus(`Done in ${secs}s · ${dur}s of audio`);
  } catch (err) {
    console.error(err);
    setStatus('Error: ' + (err?.message || String(err)));
  } finally {
    btn.disabled = false;
  }
});

// let a click anywhere start the download in the background before they ask
el('#text').addEventListener('blur', () => {
  if (!state.tts && !state.loading) ensureModel().catch(() => {});
});
