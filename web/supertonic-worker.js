// Supertonic 3 in a worker: 31 languages from raw text (no phonemizer), ten voices, from the
// files in static/{{SUPERTONIC_DIR}} (built by scripts/build_supertonic.py), run with
// onnxruntime-web. Pipeline, as Supertone's own: a duration predictor sets the length, a text
// encoder reads the text, a flow-matching estimator turns noise into a speech latent in a few
// steps, and the vocoder makes 44.1 kHz audio, resampled here to Voxa's 24 kHz.
// Weights: Supertone Inc., BigScience OpenRAIL-M (see LICENSE.txt next to the files).

const ORT = '/static/onnxruntime-web-{{ORT_VERSION}}/';
const BASE = '/static/{{SUPERTONIC_DIR}}/';
const CACHE = 'voxa-{{SUPERTONIC_DIR}}';
const MODELS = ['duration_predictor', 'text_encoder', 'vector_estimator', 'vocoder'];
const OUT_RATE = 24000;
const STEPS = 6;          // flow-matching steps: Supertone's demos use 5; one more costs ~15% and is cleaner
const SPEED = 1.05;       // Supertone's default pace

let ort, cfg, indexer, sessions, voiceIndex, voiceData, loading = null, queue = Promise.resolve();
const cancelled = new Set();

async function getFile(name, progress) {
  const url = BASE + name;
  let cache = null;
  try {
    cache = await caches.open(CACHE);
    for (const key of await caches.keys()) if (key.startsWith('voxa-supertonic-') && key !== CACHE) caches.delete(key);
  } catch { /* private browsing: no Cache Storage, use the network */ }
  const hit = cache && await cache.match(url).catch(() => null);
  if (hit) { const buf = await hit.arrayBuffer(); progress(name, buf.byteLength, true); return buf; }
  const r = await fetch(url);
  if (!r.ok || !r.body) throw new Error(`Could not download the on-device voice (${r.status}).`);
  const reader = r.body.getReader(), chunks = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value); got += value.length;
    progress(name, got, false);
  }
  const buf = new Uint8Array(got);
  let at = 0;
  for (const c of chunks) { buf.set(c, at); at += c.length; }
  if (cache) cache.put(url, new Response(buf)).catch(() => {});
  return buf.buffer;
}

async function load() {
  ort = await import(ORT + 'ort.wasm.min.mjs');
  ort.env.wasm.wasmPaths = ORT;
  ort.env.logLevel = 'error';
  ort.env.wasm.numThreads = self.crossOriginIsolated ? Math.max(1, Math.min(4, navigator.hardwareConcurrency || 1)) : 1;
  const seen = {};
  const progress = (file, loaded, cached) => {
    seen[file] = loaded;
    postMessage({ type: 'progress', file, loaded, total: Object.values(seen).reduce((a, b) => a + b, 0), cached });
  };
  const names = ['tts.json', 'unicode_indexer.json', 'voices.json', 'voices.bin', ...MODELS.map(m => m + '.onnx')];
  const bufs = await Promise.all(names.map(n => getFile(n, progress)));
  const text = b => JSON.parse(new TextDecoder().decode(b));
  cfg = text(bufs[0]); indexer = text(bufs[1]); voiceIndex = text(bufs[2]); voiceData = new Float32Array(bufs[3]);
  const opts = { executionProviders: ['wasm'], graphOptimizationLevel: 'all' };
  sessions = {};
  // One at a time: four sessions opening together would hold every model's bytes twice at once.
  for (let i = 0; i < MODELS.length; i++) sessions[MODELS[i]] = await ort.InferenceSession.create(new Uint8Array(bufs[4 + i]), opts);
  return { sampleRate: OUT_RATE, voices: Object.keys(voiceIndex.voices) };
}

// --- Text ---------------------------------------------------------------------------------------
// As Supertone's preprocessing: NFKD, tidy dashes and quotes, end with punctuation, and wrap
// in the language tag the model was trained with.
function prepare(text, lang) {
  let t = text.normalize('NFKD')
    .replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]+/gu, '')
    .replace(/[–‑—]/g, '-').replace(/[“”„«»]/g, '"').replace(/[‘’´`]/g, "'")
    .replace(/[\[\]|/#\\♥☆♡©]/g, ' ').replace(/\s+/g, ' ').trim();
  if (!/[.!?;:,'")\]}…。」』】〉》›»؟،۔।]$/.test(t)) t += '.';
  return `<${lang}>${t}</${lang}>`;
}

// Long text is said a sentence or so at a time; very long sentences are split at commas, then spaces.
const CJK = new Set(['ja', 'ko']);
function pieces(text, lang) {
  const max = CJK.has(lang) ? 120 : 280;
  const sentences = text.replace(/\s+/g, ' ').trim().match(/[^.!?。！？؟।]+[.!?。！？؟।]*["'”’)\]]*\s*/g) || [text];
  const out = [];
  let buf = '';
  const push = s => { s = s.trim(); if (s) out.push(s); };
  for (let s of sentences) {
    while (s.length > max) {
      let cut = Math.max(s.lastIndexOf(', ', max), s.lastIndexOf('، ', max), s.lastIndexOf('、', max));
      if (cut < max / 3) cut = s.lastIndexOf(' ', max);
      if (cut < max / 3) cut = max;
      push(buf); buf = '';
      push(s.slice(0, cut + 1)); s = s.slice(cut + 1);
    }
    if ((buf + s).length > max) { push(buf); buf = s; } else buf += s;
  }
  push(buf);
  return out;
}

// --- Synthesis ----------------------------------------------------------------------------------
function rng(seed) {
  let a = (Number(BigInt.asUintN(32, BigInt(Math.floor(seed)))) ^ 0x9e3779b9) >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function voiceTensors(name) {
  const v = voiceIndex.voices[name];
  if (!v) throw new Error(`Unknown Supertonic voice ${name}`);
  return {
    ttl: new ort.Tensor('float32', voiceData.slice(v.offset, v.offset + v.ttl), voiceIndex.ttl_dims),
    dp: new ort.Tensor('float32', voiceData.slice(v.offset + v.ttl, v.offset + v.ttl + v.dp), voiceIndex.dp_dims),
  };
}

const breathe = () => new Promise(r => setTimeout(r, 0));

async function piece(job, text, lang, style, random) {
  const processed = prepare(text, lang);
  const codes = [...processed];  // by code point, as the indexer is
  const ids = new BigInt64Array(codes.length), mask = new Float32Array(codes.length).fill(1);
  codes.forEach((c, i) => { const cp = c.codePointAt(0); ids[i] = BigInt(cp < indexer.length ? indexer[cp] : -1); });
  const textIds = new ort.Tensor('int64', ids, [1, codes.length]);
  const textMask = new ort.Tensor('float32', mask, [1, 1, codes.length]);
  const d = await sessions.duration_predictor.run({ text_ids: textIds, style_dp: style.dp, text_mask: textMask });
  const seconds = d.duration.data[0] / SPEED;
  const enc = await sessions.text_encoder.run({ text_ids: textIds, style_ttl: style.ttl, text_mask: textMask });
  if (cancelled.has(job)) throw new DOMException('Cancelled', 'AbortError');

  const rate = cfg.ae.sample_rate, chunk = cfg.ae.base_chunk_size * cfg.ttl.chunk_compress_factor;
  const len = Math.max(1, Math.ceil(Math.floor(seconds * rate) / chunk));
  const dim = cfg.ttl.latent_dim * cfg.ttl.chunk_compress_factor;
  let xt = new Float32Array(dim * len);
  for (let i = 0; i < xt.length; i += 2) {  // Box-Muller, two normals per pair of uniforms
    const u1 = Math.max(1e-7, random()), u2 = random(), r = Math.sqrt(-2 * Math.log(u1));
    xt[i] = r * Math.cos(2 * Math.PI * u2);
    if (i + 1 < xt.length) xt[i + 1] = r * Math.sin(2 * Math.PI * u2);
  }
  const latentMask = new ort.Tensor('float32', new Float32Array(len).fill(1), [1, 1, len]);
  const total = new ort.Tensor('float32', new Float32Array([STEPS]), [1]);
  for (let step = 0; step < STEPS; step++) {
    const out = await sessions.vector_estimator.run({
      noisy_latent: new ort.Tensor('float32', xt, [1, dim, len]), text_emb: enc.text_emb, style_ttl: style.ttl,
      latent_mask: latentMask, text_mask: textMask,
      current_step: new ort.Tensor('float32', new Float32Array([step]), [1]), total_step: total,
    });
    xt = out.denoised_latent.data;
    if (cancelled.has(job)) throw new DOMException('Cancelled', 'AbortError');
    await breathe();
  }
  const wav = (await sessions.vocoder.run({ latent: new ort.Tensor('float32', xt, [1, dim, len]) })).wav_tts.data;
  return resample(wav.subarray(0, Math.min(wav.length, Math.floor(rate * seconds))), rate, OUT_RATE);
}

// Windowed-sinc resampler (Blackman, 16 zero crossings each side), with the cutoff just
// below the lower Nyquist so nothing folds back.
function resample(x, from, to) {
  if (from === to) return Float32Array.from(x);
  const ratio = to / from, n = Math.floor(x.length * ratio), out = new Float32Array(n);
  const cutoff = Math.min(1, ratio) * 0.97, half = 16 / cutoff;
  for (let i = 0; i < n; i++) {
    const t = i / ratio, lo = Math.max(0, Math.ceil(t - half)), hi = Math.min(x.length - 1, Math.floor(t + half));
    let acc = 0, norm = 0;
    for (let j = lo; j <= hi; j++) {
      const d = j - t, a = Math.PI * d * cutoff;
      const s = d === 0 ? 1 : Math.sin(a) / a;
      const w = 0.42 + 0.5 * Math.cos(Math.PI * d / half) + 0.08 * Math.cos(2 * Math.PI * d / half);
      acc += x[j] * s * w; norm += s * w;
    }
    out[i] = norm ? acc / norm : 0;
  }
  return out;
}

async function speak(job, text, voice, opts) {
  const style = voiceTensors(voice.voice), lang = voice.lang, random = rng(opts.seed ?? 4242);
  const parts = pieces(text, lang);
  const gap = new Float32Array(Math.round(OUT_RATE * (CJK.has(lang) ? 0.15 : 0.22)));
  const out = [];
  for (let i = 0; i < parts.length; i++) {
    if (i) out.push(gap);
    out.push(await piece(job, parts[i], lang, style, random));
    postMessage({ type: 'step', job, done: i + 1, of: parts.length });
  }
  const audio = new Float32Array(out.reduce((a, p) => a + p.length, 0));
  let at = 0;
  for (const p of out) { audio.set(p, at); at += p.length; }
  return audio;
}

onmessage = async ({ data }) => {
  if (data.type === 'cancel') { cancelled.add(data.job); return; }
  if (data.type === 'load') {
    try { loading ||= load(); postMessage({ type: 'ready', ...(await loading) }); }
    catch (e) { loading = null; postMessage({ type: 'error', job: null, message: e.message || String(e) }); }
    return;
  }
  if (data.type === 'speak') {
    queue = queue.then(async () => {
      try {
        await loading;
        if (cancelled.has(data.job)) throw new DOMException('Cancelled', 'AbortError');
        const audio = await speak(data.job, data.text, data.voice, data.opts || {});
        postMessage({ type: 'audio', job: data.job, audio }, [audio.buffer]);
      } catch (e) {
        postMessage({ type: 'error', job: data.job, message: e.message || String(e), abort: e.name === 'AbortError' });
      } finally { cancelled.delete(data.job); }
    });
  }
};
