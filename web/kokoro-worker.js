// Kokoro-82M in a worker (hexgrad, Apache-2.0): the English voices of Voxa's Studio catalogue,
// run on this device. The 325 MB float32 model and its voices load from Hugging Face (a pinned
// revision of onnx-community/Kokoro-82M-v1.0-ONNX), so the voices are marked desktop only.
// Pronunciation comes from Kale TTS nano's English front end (Apache-2.0, already served from
// static/{{KALE_DIR}}), which writes Kokoro's own phoneme symbols with Kokoro's ids. The usual
// Kokoro browser package bundles eSpeak NG (GPL-3.0); this avoids it.

const ORT = '/static/onnxruntime-web-{{ORT_VERSION}}/';
const KALE = '/static/{{KALE_DIR}}/';
const CACHE = 'voxa-kokoro-82m';
const MAX_IDS = 510;

let ort, session, prep, loading = null, queue = Promise.resolve();
const voices = new Map();
const cancelled = new Set();
let received = 0;

async function getFile(url) {
  let cache = null;
  try { cache = await caches.open(CACHE); } catch { /* private browsing */ }
  const hit = cache && await cache.match(url).catch(() => null);
  if (hit) {
    const buf = await hit.arrayBuffer();
    received += buf.byteLength;
    postMessage({ type: 'progress', total: received, cached: true });
    return buf;
  }
  const r = await fetch(url);
  if (!r.ok || !r.body) throw new Error(`Could not download the desktop voice (${r.status}).`);
  const reader = r.body.getReader(), chunks = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value); got += value.length; received += value.length;
    postMessage({ type: 'progress', total: received, cached: false });
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
  const { KaleTTS } = await import(KALE + 'js/kale-tts.mjs');
  const origin = self.location.origin;
  prep = await KaleTTS.prepare(origin + KALE + 'model/', { frontend: origin + KALE + 'frontend/' });
  return { sampleRate: 24000 };
}

// The model comes with the voice (its pinned URL in voices.json), on the first speak.
let opening = null;
function model(url) {
  return (opening ||= getFile(url).then(buf => ort.InferenceSession.create(new Uint8Array(buf),
    { executionProviders: ['wasm'], graphOptimizationLevel: 'all' })).catch(e => { opening = null; throw e; }));
}

async function styleTable(url) {
  if (!voices.has(url)) voices.set(url, new Float32Array(await getFile(url)));
  return voices.get(url);
}

const breathe = () => new Promise(r => setTimeout(r, 0));

async function speak(job, text, v) {
  const accent = v.lang === 'b' ? 'en-gb' : 'en-us';
  session = await model(v.model);
  const fe = await prep._frontEnd(accent), table = await styleTable(v.url);
  const rows = table.length / 256, gap = new Float32Array(Math.round(24000 * 0.18)), parts = [];
  for (const sentence of fe.sentences(text)) {
    let rest = [...fe.ipa(sentence)];
    const chunks = [];
    while (rest.length > MAX_IDS) {
      let cut = rest.lastIndexOf(' ', MAX_IDS);
      if (cut <= 0) cut = MAX_IDS;
      chunks.push(rest.slice(0, cut).join('')); rest = rest.slice(cut + 1);
    }
    chunks.push(rest.join(''));
    for (const c of chunks) {
      if (cancelled.has(job)) throw new DOMException('Cancelled', 'AbortError');
      const ids = await prep._ids(c, accent);
      if (!ids.length) continue;
      // Kokoro's style row is chosen by the number of tokens, as kokoro-js does
      const row = Math.min(ids.length, rows - 1);
      const x = BigInt64Array.from([0, ...ids, 0].map(BigInt));
      const out = await session.run({
        input_ids: new ort.Tensor('int64', x, [1, x.length]),
        style: new ort.Tensor('float32', table.slice(row * 256, (row + 1) * 256), [1, 256]),
        speed: new ort.Tensor('float32', Float32Array.from([1]), [1]),
      });
      if (parts.length) parts.push(gap);
      parts.push(Float32Array.from(out[session.outputNames[0]].data));
      await breathe();
    }
  }
  const audio = new Float32Array(parts.reduce((a, p) => a + p.length, 0));
  let at = 0;
  for (const p of parts) { audio.set(p, at); at += p.length; }
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
        const audio = await speak(data.job, data.text, data.voice);
        postMessage({ type: 'audio', job: data.job, audio }, [audio.buffer]);
      } catch (e) {
        postMessage({ type: 'error', job: data.job, message: e.message || String(e), abort: e.name === 'AbortError' });
      } finally { cancelled.delete(data.job); }
    });
  }
};
