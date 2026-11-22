// Pocket TTS in a worker: Kyutai's 100M-parameter voice (CC-BY-4.0) in English, French,
// German, Spanish, Portuguese or Italian, run by a Candle WebAssembly build of
// idle-intelligence/tts-web (MIT; static/{{POCKET_WASM_DIR}}, from vendor/pocket-tts-wasm).
// Each language is its own ~134 MB model (8-bit GGUF) and each voice a ~6 MB embedding, both
// from Hugging Face at pinned revisions (the URLs come with the voice, from voices.json).
// One language's model is kept loaded; a voice in another language swaps it.

const WASM = '/static/{{POCKET_WASM_DIR}}/';
const CACHE = 'voxa-pocket-tts';

let wasm = null, model = null, tokenizer = null, current = null, voices = {};
let loading = null, queue = Promise.resolve();
const cancelled = new Set();
let received = 0;

// Files come from Hugging Face at a pinned revision (…/resolve/<revision>/<path>). When Voxa moves
// to a new revision, the same file's older copy is deleted once the new one is saved, so a
// device keeps one copy of each file.
const sameFile = u => u.replace(/\/resolve\/[^/]+\//, '/resolve/~/');
async function prune(cache, url) {
  for (const req of await cache.keys()) if (req.url !== url && sameFile(req.url) === sameFile(url)) await cache.delete(req);
}

async function getFile(url) {
  let cache = null;
  try { cache = await caches.open(CACHE); } catch { /* private browsing */ }
  const hit = cache && await cache.match(url).catch(() => null);
  if (hit) {
    const buf = await hit.arrayBuffer();
    received += buf.byteLength;
    postMessage({ type: 'progress', total: received, cached: true });
    return new Uint8Array(buf);
  }
  const r = await fetch(url);
  if (!r.ok || !r.body) throw new Error(`Could not download the on-device voice (${r.status}).`);
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
  if (cache) cache.put(url, new Response(buf, { headers: { 'Content-Length': String(buf.length) } }))
    .then(() => prune(cache, url)).catch(() => {});
  return buf;
}

async function load() {
  wasm = await import(WASM + 'tts_wasm.js');
  await wasm.default({ module_or_path: WASM + 'tts_wasm_bg.wasm' });
  return { sampleRate: 24000 };
}

// The model for a voice's language, loaded (and the previous one freed) when it changes.
async function useLanguage(v) {
  if (current === v.code) return;
  received = 0;
  const [tok, weights] = await Promise.all([getFile(v.tokenizer), getFile(v.model)]);
  if (model) { model.free(); model = null; }
  if (tokenizer) { tokenizer.free(); tokenizer = null; }
  tokenizer = new wasm.Tokenizer(tok);
  model = new wasm.Model(weights, v.code);
  current = v.code; voices = {};
}

async function voiceIndex(v) {
  if (voices[v.source] != null) return voices[v.source];
  return (voices[v.source] = model.add_voice(await getFile(v.url)));
}

// Long text is said a sentence or so at a time, joined with a short pause.
function pieces(text, max = 260) {
  const sentences = text.replace(/\s+/g, ' ').trim().match(/[^.!?]+[.!?]*["'”’)\]]*\s*/g) || [text];
  const out = [];
  let buf = '';
  for (let s of sentences) {
    while (s.length > max) {
      let cut = s.lastIndexOf(', ', max);
      if (cut < max / 3) cut = s.lastIndexOf(' ', max);
      if (cut < max / 3) cut = max;
      if (buf.trim()) out.push(buf.trim());
      buf = '';
      out.push(s.slice(0, cut + 1).trim()); s = s.slice(cut + 1);
    }
    if ((buf + s).length > max && buf.trim()) { out.push(buf.trim()); buf = s; } else buf += s;
  }
  if (buf.trim()) out.push(buf.trim());
  return out;
}

const breathe = () => new Promise(r => setTimeout(r, 0));

async function speak(job, text, v, { temperature = 0.7 }) {
  await useLanguage(v);
  const index = await voiceIndex(v);
  const parts = [], gap = new Float32Array(Math.round(24000 * 0.2));
  for (const [i, piece] of pieces(text).entries()) {
    if (i) parts.push(gap);
    const [prepared, framesAfterEos] = model.prepare_text(piece);
    model.start_generation(index, tokenizer.encode(prepared), framesAfterEos, temperature);
    for (let step = 0; ; step++) {
      if (cancelled.has(job)) { model.cancel_generation(); throw new DOMException('Cancelled', 'AbortError'); }
      const chunk = model.generation_step();
      if (!chunk) break;
      parts.push(chunk);
      if (step % 8 === 7) await breathe();
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
        const audio = await speak(data.job, data.text, data.voice, data.opts || {});
        postMessage({ type: 'audio', job: data.job, audio }, [audio.buffer]);
      } catch (e) {
        postMessage({ type: 'error', job: data.job, message: e.message || String(e), abort: e.name === 'AbortError' });
      } finally { cancelled.delete(data.job); }
    });
  }
};
