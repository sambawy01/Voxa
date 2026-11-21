// Kale TTS nano in a worker: a 7.5M-parameter English voice with ten designed voices
// (KaleComputer, Apache-2.0), from static/{{KALE_DIR}} (fetched by scripts/build_kale.py), run
// with onnxruntime-web through Kale's own runtime (js/kale-tts.mjs) and English front end.
// Output is 24 kHz mono, as the other on-device voices.

const ORT = '/static/onnxruntime-web-{{ORT_VERSION}}/';
const BASE = '/static/{{KALE_DIR}}/';
const CACHE = 'voxa-{{KALE_DIR}}';

let tts = null, loading = null, queue = Promise.resolve();
const cancelled = new Set();

// Kale's runtime fetches its own files; they are kept in Cache Storage so the ~25 MB come once.
const nativeFetch = self.fetch.bind(self);
let received = 0, allCached = true;
self.fetch = async (input, init) => {
  const url = new URL(String(input?.url || input), self.location.href).href;
  if (!url.startsWith(self.location.origin + BASE)) return nativeFetch(input, init);
  let cache = null;
  try {
    cache = await caches.open(CACHE);
    for (const key of await caches.keys()) if (key.startsWith('voxa-kale-') && key !== CACHE) caches.delete(key);
  } catch { /* private browsing */ }
  const hit = cache && await cache.match(url).catch(() => null);
  if (hit) {
    const buf = await hit.clone().arrayBuffer();
    received += buf.byteLength;
    postMessage({ type: 'progress', total: received, cached: allCached });
    return hit;
  }
  allCached = false;
  const r = await nativeFetch(input, init);
  if (!r.ok) return r;
  const buf = await r.arrayBuffer();
  received += buf.byteLength;
  postMessage({ type: 'progress', total: received, cached: false });
  const stored = new Response(buf, { headers: { 'content-type': r.headers.get('content-type') || 'application/octet-stream' } });
  if (cache) cache.put(url, stored.clone()).catch(() => {});
  return stored;
};

async function load() {
  const ort = await import(ORT + 'ort.wasm.min.mjs');
  ort.env.wasm.wasmPaths = ORT;
  ort.env.logLevel = 'error';
  ort.env.wasm.numThreads = self.crossOriginIsolated ? Math.max(1, Math.min(4, navigator.hardwareConcurrency || 1)) : 1;
  const { KaleTTS } = await import(BASE + 'js/kale-tts.mjs');
  tts = await KaleTTS.load(self.location.origin + BASE + 'model/', {
    ort, frontend: self.location.origin + BASE + 'frontend/', graph: 'int8', executionProviders: ['wasm'],
    sessionOptions: { graphOptimizationLevel: 'all' },
  });
  return { sampleRate: tts.sampleRate, voices: tts.voices.map(v => v.id) };
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
        const result = await tts.speak(data.text, { voice: data.voice.source });
        if (cancelled.has(data.job)) throw new DOMException('Cancelled', 'AbortError');
        const audio = Float32Array.from(result.audio);
        postMessage({ type: 'audio', job: data.job, audio }, [audio.buffer]);
      } catch (e) {
        postMessage({ type: 'error', job: data.job, message: e.message || String(e), abort: e.name === 'AbortError' });
      } finally { cancelled.delete(data.job); }
    });
  }
};
