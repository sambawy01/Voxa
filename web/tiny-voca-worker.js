// tiny-voca in a worker: a 4.5M-parameter English voice (two voices, Apache-2.0) from the files in
// static/{{TINYVOCA_DIR}} (built by scripts/build_tiny_voca.py), run with onnxruntime-web.
// Its phonemizer, dizione, is Python, so that runs here too, under Pyodide, with the model's own
// lexicon: the same code the voice was trained with, so words come out as it expects.
// A classic worker (not a module) because Pyodide loads itself with importScripts.
importScripts('/tiny-voca-text.js', '/static/{{TINYVOCA_DIR}}/pyodide/pyodide.js');

const ORT = '/static/onnxruntime-web-{{ORT_VERSION}}/';
const BASE = '/static/{{TINYVOCA_DIR}}/';
const CACHE = 'voxa-{{TINYVOCA_DIR}}';
const { sentences, finished, shorter, limitPeak } = self.TinyVocaText;
const SAMPLE_RATE = 24000, GAP_SECONDS = 0.2;
const TOKEN_LIMIT = 512 - 12;   // the model's limit, with room for the start and end tokens

let ort, config, session, phonemize, loading = null, queue = Promise.resolve();
const cancelled = new Set();
let received = 0, allCached = true;

const progress = (bytes, cached) => {
  received += bytes; allCached = allCached && cached;
  postMessage({ type: 'progress', total: received, cached: allCached });
};

// Files are kept in Cache Storage, which outlasts the ordinary HTTP cache, so the download
// happens once. Older builds' caches are removed.
async function getFile(name) {
  const url = BASE + name;
  let cache = null;
  try {
    cache = await caches.open(CACHE);
    for (const key of await caches.keys()) if (key.startsWith('voxa-tiny-voca-') && key !== CACHE) caches.delete(key);
  } catch { /* private browsing: no Cache Storage, use the network */ }
  const hit = cache && await cache.match(url).catch(() => null);
  if (hit) { const buf = await hit.arrayBuffer(); progress(buf.byteLength, true); return buf; }
  const buf = await download(url);
  if (cache) cache.put(url, new Response(buf)).catch(() => {});
  return buf;
}

async function download(url) {
  const r = await fetch(url);
  if (!r.ok || !r.body) throw new Error(`Could not download the on-device voice (${r.status}).`);
  const reader = r.body.getReader(), chunks = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value); got += value.length;
    progress(value.length, false);
  }
  const buf = new Uint8Array(got);
  let at = 0;
  for (const c of chunks) { buf.set(c, at); at += c.length; }
  return buf.buffer;
}

// Pyodide fetches its own files; read them once here first so the download shows up in the
// progress, and Pyodide's own requests are then answered from the browser's cache (they are
// served immutable).
async function warm(names) {
  for (const name of names) {
    const r = await fetch(BASE + 'pyodide/' + name);
    if (!r.ok || !r.body) throw new Error(`Could not download the on-device voice (${r.status}).`);
    const reader = r.body.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      progress(value.length, false);
    }
  }
}

async function load() {
  ort = await import(ORT + 'ort.wasm.min.mjs');
  ort.env.wasm.wasmPaths = ORT;
  ort.env.logLevel = 'error';
  // One thread: the model is tiny, and every extra thread costs memory, which phones are short of.
  ort.env.wasm.numThreads = 1;

  const [model, cfg, data, code] = await Promise.all(['model.onnx', 'config.json', 'phonemizer.zip', 'dizione.zip'].map(getFile));
  config = JSON.parse(new TextDecoder().decode(cfg));
  // The speech runtime goes first. It asks the browser for one large block of memory, which
  // phones (iOS Safari) refuse once Python has taken its share; the phonemizer is more flexible.
  session = await ort.InferenceSession.create(new Uint8Array(model), { executionProviders: ['wasm'], graphOptimizationLevel: 'all' });
  await warm(['pyodide.asm.wasm', 'python_stdlib.zip', 'numpy-1.26.4-cp312-cp312-pyodide_2024_0_wasm32.whl']);

  const py = await loadPyodide({ indexURL: BASE + 'pyodide/' });
  await py.loadPackage('numpy');
  py.unpackArchive(data, 'zip', { extractDir: '/tinyvoca/data' });
  py.unpackArchive(code, 'zip', { extractDir: '/tinyvoca/code' });
  py.runPython(`
import json, os, sys
os.environ["DIZIONE_DATA"] = "/tinyvoca/data"
sys.path.insert(0, "/tinyvoca/code")
import dizione

def phonemize_json(text):
    try:
        result = dizione.phonemize(text)
    except ValueError:
        # Nothing pronounceable (another script, signs only): the caller skips the piece.
        if any(word for mark, words in dizione.pieces(text) if not mark for word in words):
            raise
        return "null"
    return json.dumps({"ids": list(result.ids), "stress": list(result.stress)})

phonemize_json("Warm up.")
`);
  phonemize = py.globals.get('phonemize_json');
  await synthesize(phonemes('Hello.'), 0);   // the first run is slower; do it before anyone waits
  return { voices: config.model.speakers };
}

function phonemes(text) {
  try { return JSON.parse(phonemize(text)); } catch { return null; }
}

async function synthesize(phones, voice) {
  const n = phones.ids.length;
  const ints = values => new ort.Tensor('int64', BigInt64Array.from(values, BigInt), [1, n]);
  const scalar = value => new ort.Tensor('float32', Float32Array.of(value), [1]);
  const out = await session.run({
    tokens: ints(phones.ids), stress: ints(phones.stress),
    voice: new ort.Tensor('int64', BigInt64Array.of(BigInt(voice)), [1]),
    speed: scalar(1), pitch_shift: scalar(0), pitch_range: scalar(1),
  });
  const audio = out.audio.data;
  for (let i = 0; i < audio.length; i++) if (!Number.isFinite(audio[i])) throw new Error('The on-device voice produced no sound.');
  return limitPeak(Float32Array.from(audio));
}

// A sentence too long for one call is cut at a clause or in half, until each part fits.
function collect(piece, out) {
  const phones = phonemes(piece);
  if (!phones) return;
  if (phones.ids.length <= TOKEN_LIMIT) { out.push(phones); return; }
  const parts = shorter(piece);
  if (!parts.length) throw new Error(`One word is too long to speak: ${piece.slice(0, 40)}`);
  for (const part of parts) collect(part, out);
}

async function speak(job, text, speaker) {
  const voice = Math.max(0, config.model.speakers.indexOf(speaker));
  const pieces = [];
  for (const sentence of sentences(finished(text))) collect(sentence, pieces);
  if (!pieces.length) throw new Error('Nothing in the text could be read: this voice reads English.');
  const gap = new Float32Array(Math.round(SAMPLE_RATE * GAP_SECONDS)), parts = [];
  const started = performance.now();
  for (let i = 0; i < pieces.length; i++) {
    if (cancelled.has(job)) throw new DOMException('Cancelled', 'AbortError');
    if (i) parts.push(gap);
    parts.push(await synthesize(pieces[i], voice));
  }
  const audio = new Float32Array(parts.reduce((a, p) => a + p.length, 0));
  let at = 0;
  for (const p of parts) { audio.set(p, at); at += p.length; }
  return { audio, timings: { seconds: (performance.now() - started) / 1000, audioSeconds: audio.length / SAMPLE_RATE } };
}

onmessage = async ({ data }) => {
  if (data.type === 'cancel') { cancelled.add(data.job); return; }
  if (data.type === 'load') {
    try { loading ||= load(); postMessage({ type: 'ready', ...(await loading) }); }
    catch (e) { loading = null; postMessage({ type: 'error', job: null, message: e.message || String(e) }); }
    return;
  }
  if (data.type === 'speak') {
    // One generation at a time: the session is shared.
    queue = queue.then(async () => {
      try {
        await loading;
        const { audio, timings } = await speak(data.job, data.text, data.voice);
        postMessage({ type: 'audio', job: data.job, audio, timings }, [audio.buffer]);
      } catch (e) {
        postMessage({ type: 'error', job: data.job, message: e.message || String(e), abort: e.name === 'AbortError' });
      } finally { cancelled.delete(data.job); }
    });
  }
};
