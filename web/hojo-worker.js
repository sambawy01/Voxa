// Hojo TTS Light 40M in a worker: the files from static/{{HOJO_DIR}} (built by
// scripts/build_hojo.py) run with onnxruntime-web. A port of Hojo's own ONNX runtime:
// a small language model writes audio tokens, a fine stage turns them into codec bits,
// the decoder makes a spectrogram, and an inverse STFT here makes the waveform.
import { HojoTokenizer, promptIds } from '/hojo-text.js';

const ORT = '/static/onnxruntime-web-{{ORT_VERSION}}/';
const BASE = '/static/{{HOJO_DIR}}/';
const CACHE = 'voxa-{{HOJO_DIR}}';
const FILES = { meta: 'hojo.json', voices: 'voices.bin', llm: 'llm.onnx', fine: 'fine.onnx', decoder: 'decoder.onnx' };

let ort, meta, tokenizer, llm, fine, decoder, tokenEmbedding, speakerEmbeds, speakerVecs;
let loading = null, timings = null, queue = Promise.resolve();
const cancelled = new Set();

// Model files are kept in Cache Storage, which outlasts the ordinary HTTP cache, so the
// ~126 MB download happens once. Older builds' caches are removed.
async function getFile(name, progress) {
  const url = BASE + name;
  let cache = null;
  try {
    cache = await caches.open(CACHE);
    for (const key of await caches.keys()) if (key.startsWith('voxa-hojo-') && key !== CACHE) caches.delete(key);
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
  const [m, v, a, b, c] = await Promise.all([FILES.meta, FILES.voices, FILES.llm, FILES.fine, FILES.decoder].map(f => getFile(f, progress)));
  meta = JSON.parse(new TextDecoder().decode(m));
  tokenizer = new HojoTokenizer(meta);
  const L = meta.layout, H = meta.hidden;
  tokenEmbedding = new Uint16Array(v, L.token_embedding[0], meta.vocab * H);
  speakerEmbeds = new Float32Array(v, L.speaker_embeds[0], meta.voices.length * meta.slots * H);
  speakerVecs = new Float32Array(v, L.speaker_vecs[0], meta.voices.length * meta.speaker_dim);
  const opts = { executionProviders: ['wasm'], graphOptimizationLevel: 'all' };
  [llm, fine, decoder] = await Promise.all([a, b, c].map(buf => ort.InferenceSession.create(new Uint8Array(buf), opts)));
  return { voices: meta.voices, sampleRate: meta.sample_rate };
}

// float16 -> float32, for rows of the token embedding table.
const F16 = new Float32Array(65536);
for (let h = 0; h < 65536; h++) {
  const s = h & 0x8000 ? -1 : 1, e = (h >> 10) & 0x1f, f = h & 0x3ff;
  F16[h] = e === 0 ? s * 2 ** -14 * (f / 1024) : e === 31 ? (f ? NaN : s * Infinity) : s * 2 ** (e - 15) * (1 + f / 1024);
}
function embedRows(ids, out = new Float32Array(ids.length * meta.hidden)) {
  const H = meta.hidden;
  ids.forEach((id, r) => { const src = id * H, dst = r * H; for (let i = 0; i < H; i++) out[dst + i] = F16[tokenEmbedding[src + i]]; });
  return out;
}

// A small seeded generator (mulberry32), so a take can be repeated exactly.
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

// Hojo's sampler: repetition penalty over every token so far, temperature, then top-p.
function sample(logits, off, V, used, { temperature, topP, penalty }, random) {
  const row = new Float32Array(V);
  for (let i = 0; i < V; i++) row[i] = logits[off + i];
  for (const id of used) row[id] = row[id] > 0 ? row[id] / penalty : row[id] * penalty;
  if (temperature <= 0) { let best = 0; for (let i = 1; i < V; i++) if (row[i] > row[best]) best = i; return best; }
  let max = -Infinity;
  for (let i = 0; i < V; i++) { row[i] /= temperature; if (row[i] > max) max = row[i]; }
  let sum = 0;
  for (let i = 0; i < V; i++) { row[i] = Math.exp(row[i] - max); sum += row[i]; }
  const keep = [];
  for (let i = 0; i < V; i++) if (row[i] / sum > 1e-7) keep.push(i);
  keep.sort((x, y) => row[y] - row[x]);
  let total = 0, n = 0;
  for (; n < keep.length; n++) { total += row[keep[n]] / sum; if (total > topP) { n++; break; } }
  const chosen = keep.slice(0, Math.max(1, n));
  let mass = 0;
  for (const i of chosen) mass += row[i];
  let r = random() * mass;
  for (const i of chosen) { r -= row[i]; if (r <= 0) return i; }
  return chosen[chosen.length - 1];
}

const tensor = (type, data, dims) => new ort.Tensor(type, data, dims);
const ids64 = arr => BigInt64Array.from(arr, BigInt);
const breathe = () => new Promise(r => setTimeout(r, 0));

async function speak(job, text, voice, opts) {
  const V = meta.vocab, H = meta.hidden, layers = meta.layers;
  const v = meta.voices.indexOf(voice);
  if (v < 0) throw new Error(`Unknown on-device voice ${voice}`);
  const prompt = promptIds(tokenizer, meta, text);
  const embeds = embedRows(prompt);
  const slots = meta.speaker_slots;
  prompt.forEach((id, r) => {
    const k = slots.indexOf(id);
    if (k >= 0) embeds.set(speakerEmbeds.subarray((v * meta.slots + k) * H, (v * meta.slots + k + 1) * H), r * H);
  });
  const random = rng(opts.seed);
  const clock = { start: performance.now() };
  const maxNew = Math.min(2048, meta.max_positions - prompt.length);
  const kvDims = len => [1, meta.kv_heads, len, meta.head_dim];
  let feeds = { inputs_embeds: tensor('float32', embeds, [1, prompt.length, H]),
                position_ids: tensor('int64', ids64(prompt.map((_, i) => i)), [1, prompt.length]) };
  for (let l = 0; l < layers; l++) {
    feeds[`past_key_values.${l}.key`] = tensor('float32', new Float32Array(0), kvDims(0));
    feeds[`past_key_values.${l}.value`] = tensor('float32', new Float32Array(0), kvDims(0));
  }
  const hidden = new Float32Array((prompt.length + maxNew) * H);
  const generated = [], used = new Set();
  let pos = 0, out = null;
  const end = meta.special['[target_speech_end]'];
  for (let step = 0; step < maxNew; step++) {
    if (cancelled.has(job)) throw new DOMException('Cancelled', 'AbortError');
    const prev = out;
    out = await llm.run(feeds);
    if (prev) for (const t of Object.values(prev)) t.dispose?.();
    const n = out.last_hidden_state.dims[1];
    hidden.set(out.last_hidden_state.data, pos * H);
    pos += n;
    const next = sample(out.logits.data, (n - 1) * V, V, used, opts, random);
    generated.push(next); used.add(next);
    if (next === end && generated.length >= 10) break;
    feeds = { inputs_embeds: tensor('float32', embedRows([next]), [1, 1, H]),
              position_ids: tensor('int64', ids64([prompt.length + step]), [1, 1]) };
    for (let l = 0; l < layers; l++) {
      feeds[`past_key_values.${l}.key`] = out[`present.${l}.key`];
      feeds[`past_key_values.${l}.value`] = out[`present.${l}.value`];
    }
    if (step % 16 === 15) { postMessage({ type: 'step', job, frames: generated.length }); await breathe(); }
  }
  if (out) for (const t of Object.values(out)) t.dispose?.();
  clock.lm = performance.now();

  // Audio tokens before the end marker, at their positions in the whole sequence.
  const stop = generated.indexOf(end);
  const seq = stop >= 0 ? generated.slice(0, stop) : generated;
  const first = meta.audio_first, last = first + meta.audio_count;
  const positions = [], coarse = [];
  seq.forEach((id, rel) => { if (id >= first && id < last) { positions.push(prompt.length + rel); coarse.push(id); } });
  if (!positions.length) return new Float32Array(0);
  const F = positions.length;
  const hs = new Float32Array(F * H);
  positions.forEach((p, i) => hs.set(hidden.subarray((p - 1) * H, p * H), i * H));
  const fineOut = await fine.run({
    hidden_states: tensor('float32', hs, [1, F, H]),
    coarse_embeddings: tensor('float32', embedRows(coarse), [1, F, H]),
    speaker_embedding: tensor('float32', speakerVecs.slice(v * meta.speaker_dim, (v + 1) * meta.speaker_dim), [1, meta.speaker_dim]),
    valid_mask: tensor('bool', new Uint8Array(F).fill(1), [1, F]),
  });
  clock.fine = performance.now();
  const logits = fineOut.binary_logits;
  const bits = new Float32Array(logits.data.length);
  for (let i = 0; i < bits.length; i++) bits[i] = logits.data[i] > 0 ? 1 : -1;
  const dec = await decoder.run({ bits: tensor('float32', bits, [logits.dims[1], logits.dims[2]]) });
  clock.decoder = performance.now();
  const audio = istft(dec.mag.data, dec.phase.data, dec.mag.dims[0], dec.mag.dims[1]);
  timings = { steps: generated.length, lm: clock.lm - clock.start, fine: clock.fine - clock.lm,
              decoder: clock.decoder - clock.fine, istft: performance.now() - clock.decoder };
  return audio;
}

// --- Inverse STFT: mag/phase [bins, frames] -> waveform, as Hojo's NumPy ISTFT ("same" padding).
let fftPlan = null;
function plan(n) {
  if (fftPlan && fftPlan.n === n) return fftPlan;
  const factors = [];
  for (let m = n, f = 2; m > 1;) { if (m % f === 0) { factors.push(f); m /= f; } else f++; }
  const cos = new Float64Array(n), sin = new Float64Array(n);
  for (let k = 0; k < n; k++) { cos[k] = Math.cos(2 * Math.PI * k / n); sin[k] = Math.sin(2 * Math.PI * k / n); }
  const win = new Float32Array(n);
  for (let i = 0; i < n; i++) win[i] = 0.5 - 0.5 * Math.cos(2 * Math.PI * i / n);  // periodic Hann
  return (fftPlan = { n, factors, cos, sin, win });
}

// Mixed-radix inverse DFT (e^{+i}), unnormalized: split into r interleaved subsequences by
// the next prime factor r of n, transform each, then combine with twiddles from the table.
function idft(re, im, n, depth, p, outRe, outIm) {
  if (n === 1) { outRe[0] = re[0]; outIm[0] = im[0]; return; }
  const r = p.factors[depth], m = n / r, step = p.n / n;
  const subRe = new Float64Array(n), subIm = new Float64Array(n);
  const inRe = new Float64Array(m), inIm = new Float64Array(m);
  for (let q = 0; q < r; q++) {
    for (let j = 0; j < m; j++) { inRe[j] = re[q + r * j]; inIm[j] = im[q + r * j]; }
    idft(inRe, inIm, m, depth + 1, p, subRe.subarray(q * m, (q + 1) * m), subIm.subarray(q * m, (q + 1) * m));
  }
  for (let k = 0; k < n; k++) {
    let sr = 0, si = 0;
    const km = k % m;
    for (let q = 0; q < r; q++) {
      const t = ((q * k) % n) * step, c = p.cos[t], s = p.sin[t];
      const xr = subRe[q * m + km], xi = subIm[q * m + km];
      sr += xr * c - xi * s; si += xr * s + xi * c;
    }
    outRe[k] = sr; outIm[k] = si;
  }
}

function istft(mag, phase, bins, frames) {
  const n = (bins - 1) * 2, hop = meta.hop, p = plan(n);
  const pad = (n - hop) / 2, total = (frames - 1) * hop + n;
  const y = new Float32Array(total), env = new Float32Array(total);
  const re = new Float64Array(n), im = new Float64Array(n), oRe = new Float64Array(n), oIm = new Float64Array(n);
  for (let t = 0; t < frames; t++) {
    for (let k = 0; k < bins; k++) {
      const a = Math.min(Math.max(mag[k * frames + t], 1e-12), 100), ph = phase[k * frames + t];
      re[k] = a * Math.cos(ph); im[k] = (k === 0 || k === bins - 1) ? 0 : a * Math.sin(ph);
    }
    for (let k = 1; k < bins - 1; k++) { re[n - k] = re[k]; im[n - k] = -im[k]; }
    idft(re, im, n, 0, p, oRe, oIm);
    const at = t * hop;
    for (let i = 0; i < n; i++) { y[at + i] += (oRe[i] / n) * p.win[i]; env[at + i] += p.win[i] * p.win[i]; }
  }
  const out = new Float32Array(total - 2 * pad);
  for (let i = 0; i < out.length; i++) out[i] = y[i + pad] / env[i + pad];
  return out;
}

onmessage = async ({ data }) => {
  if (data.type === 'cancel') { cancelled.add(data.job); return; }
  if (data.type === 'load') {
    try { loading ||= load(); postMessage({ type: 'ready', ...(await loading) }); }
    catch (e) { loading = null; postMessage({ type: 'error', job: null, message: e.message || String(e) }); }
    return;
  }
  if (data.type === 'speak') {
    // One generation at a time: the sessions are shared, and a run must finish before the next.
    queue = queue.then(async () => {
      try {
        await loading;
        const audio = await speak(data.job, data.text, data.voice, data.opts);
        postMessage({ type: 'audio', job: data.job, audio, timings }, [audio.buffer]);
      } catch (e) {
        postMessage({ type: 'error', job: data.job, message: e.message || String(e), abort: e.name === 'AbortError' });
      } finally { cancelled.delete(data.job); }
    });
  }
};
