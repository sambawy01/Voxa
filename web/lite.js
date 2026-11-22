// On-device voices, shared by every page: three engines behind one call.
//   Phonon 31m (Gradium) for its English voices: ~65 MB, loaded from Hugging Face.
//   Hojo TTS Light 40M (HojoAI) for Voxa's other English voices and Chinese: ~126 MB, served
//   from static/ and run in web/hojo-worker.js.
//   tiny-voca (4.5M, English, two voices): ~50 MB with its Python phonemizer, served from static/
//   and run in web/tiny-voca-worker.js. By far the fastest of the three.
//   Supertonic 3 (Supertone, 31 languages, ten voices): ~111 MB, static/, web/supertonic-worker.js.
//   Pocket TTS (Kyutai; English, French, German, Spanish, Portuguese, Italian): ~134 MB per
//   language from Hugging Face, run by a Candle WebAssembly build in web/pocket-worker.js.
//   Kale TTS nano (KaleComputer, English, ten voices): ~25 MB, static/, web/kale-worker.js.
//   SmollerTTS (2.8M, English, one voice): ~30 MB with its own runtime, run by its own worker in static/.
// Each engine loads the first time one of its voices speaks, then comes from the cache.
import { normalizeForHojo, splitForHojo } from '/hojo-text.js';

const ABOUT_MB = { phonon: 65, hojo: 126, tinyvoca: 50, supertonic: 111, pocket: 140, kale: 29, smoller: 30 };

function statusText(engine, files, cached) {
  const mb = Object.values(files).reduce((a, b) => a + b, 0) / 1e6;
  return cached ? 'Loading the on-device voice…'
    : `Downloading the on-device voice (once)… ${mb.toFixed(0)} MB of about ${ABOUT_MB[engine]}`;
}

// A model in its own worker: loads once, then says one piece of text at a time.
class WorkerEngine {
  constructor(engine, url, options) {
    this.worker = new Worker(url, options);
    this.jobs = new Map();
    this.next = 1;
    this.onStatus = () => {};
    this.ready = new Promise((resolve, reject) => { this.loaded = { resolve, reject }; });
    this.worker.onmessage = ({ data }) => {
      if (data.type === 'progress') return this.onStatus(statusText(engine, { all: data.total }, data.cached));
      if (data.type === 'ready') return this.loaded.resolve(data);
      const job = this.jobs.get(data.job);
      if (data.type === 'error' && !job) return this.loaded.reject(new Error(data.message));
      if (!job) return;
      if (data.type === 'audio') { this.jobs.delete(data.job); this.timings = data.timings; job.resolve(data.audio); }
      else if (data.type === 'error') {
        this.jobs.delete(data.job);
        job.reject(data.abort ? new DOMException('Cancelled', 'AbortError') : new Error(data.message));
      }
    };
    this.worker.onerror = e => this.loaded.reject(new Error(e.message || 'The on-device voice could not start.'));
    this.worker.postMessage({ type: 'load' });
  }

  piece(text, voice, opts, signal) {
    if (signal?.aborted) return Promise.reject(new DOMException('Cancelled', 'AbortError'));
    const job = this.next++;
    return new Promise((resolve, reject) => {
      this.jobs.set(job, { resolve, reject });
      signal?.addEventListener('abort', () => this.worker.postMessage({ type: 'cancel', job }), { once: true });
      this.worker.postMessage({ type: 'speak', job, text, voice, opts });
    });
  }
}

class Hojo extends WorkerEngine {
  constructor() { super('hojo', '/hojo-worker.js', { type: 'module' }); }

  // Long text is said a few sentences at a time, joined with a short pause.
  async synth(text, voice, lang, { temperature, seed, signal }) {
    await this.ready;
    const pieces = splitForHojo(normalizeForHojo(text, lang), lang);
    const gap = new Float32Array(Math.round(24000 * (lang === 'z' ? 0.12 : 0.18)));
    const parts = [];
    for (let i = 0; i < pieces.length; i++) {
      if (i) parts.push(gap);
      parts.push(await this.piece(pieces[i], voice, { temperature, topP: 0.95, penalty: 1.1, seed: seed + i }, signal));
    }
    const out = new Float32Array(parts.reduce((a, p) => a + p.length, 0));
    let at = 0;
    for (const p of parts) { out.set(p, at); at += p.length; }
    return out;
  }
}

// tiny-voca splits long text itself, so a whole text goes to the worker in one job.
class TinyVoca extends WorkerEngine {
  constructor() { super('tinyvoca', '/tiny-voca-worker.js'); }

  async synth(text, voice, { signal }) {
    await this.ready;
    return this.piece(text, voice, {}, signal);
  }
}

// Supertonic and Kale split long text themselves; one job per text.
class Supertonic extends WorkerEngine {
  constructor() { super('supertonic', '/supertonic-worker.js', { type: 'module' }); }
  async synth(text, voice, { seed, signal }) {
    await this.ready;
    return this.piece(text, { voice: voice.source, lang: voice.code }, { seed }, signal);
  }
}

class Kale extends WorkerEngine {
  constructor() { super('kale', '/kale-worker.js', { type: 'module' }); }
  async synth(text, voice, { signal }) {
    await this.ready;
    return this.piece(text, { source: voice.source }, {}, signal);
  }
}

// Pocket TTS keeps one language's model loaded; another language's voice swaps it in.
class Pocket extends WorkerEngine {
  constructor() { super('pocket', '/pocket-worker.js', { type: 'module' }); }
  async synth(text, voice, { temperature, seed, signal }) {
    await this.ready;
    return this.piece(text, { code: voice.code, source: voice.source, model: voice.model, tokenizer: voice.tokenizer, url: voice.url },
      { temperature, seed }, signal);
  }
}

// Linear-phase windowed-sinc resampler for engines that are not 24 kHz (SmollerTTS: 22.05 kHz).
function resample(x, from, to) {
  if (from === to) return x;
  const ratio = to / from, n = Math.floor(x.length * ratio), out = new Float32Array(n);
  const cutoff = Math.min(1, ratio) * 0.97, half = 16 / cutoff;
  for (let i = 0; i < n; i++) {
    const t = i / ratio, lo = Math.max(0, Math.ceil(t - half)), hi = Math.min(x.length - 1, Math.floor(t + half));
    let acc = 0, norm = 0;
    for (let j = lo; j <= hi; j++) {
      const d = j - t, a = Math.PI * d * cutoff, s = d === 0 ? 1 : Math.sin(a) / a;
      const w = 0.42 + 0.5 * Math.cos(Math.PI * d / half) + 0.08 * Math.cos(2 * Math.PI * d / half);
      acc += x[j] * s * w; norm += s * w;
    }
    out[i] = norm ? acc / norm : 0;
  }
  return out;
}

// SmollerTTS runs its own released worker, which streams a text's audio in chunks at 22.05 kHz.
class Smoller {
  constructor(url) {
    this.worker = new Worker(url, { type: 'module' });
    this.onStatus = () => {};
    this.jobs = new Map(); this.next = 1;
    this.ready = new Promise((resolve, reject) => {
      this.worker.onmessage = ({ data }) => {
        if (data.type === 'ready') return resolve();
        if (data.type === 'load-error') return reject(new Error(data.message));
        const job = this.jobs.get(data.id);
        if (!job) return;
        if (data.type === 'chunk') job.parts.push(data.wave);
        else if (data.type === 'complete' || data.type === 'cancelled' || data.type === 'error') {
          this.jobs.delete(data.id);
          if (data.type === 'error') job.reject(new Error(data.message));
          else if (data.type === 'cancelled') job.reject(new DOMException('Cancelled', 'AbortError'));
          else {
            const gap = new Float32Array(Math.round(22050 * 0.12)), all = [];
            job.parts.forEach((p, i) => { if (i) all.push(gap); all.push(p); });
            const pcm = new Float32Array(all.reduce((a, p) => a + p.length, 0));
            let at = 0; for (const p of all) { pcm.set(p, at); at += p.length; }
            job.resolve(resample(pcm, 22050, 24000));
          }
        }
      };
      this.worker.onerror = e => reject(new Error(e.message || 'The on-device voice could not start.'));
    });
    this.onStatus('Downloading the on-device voice (once)… about 30 MB');
  }
  async synth(text, voice, { signal }) {
    await this.ready;
    const id = this.next++;
    return new Promise((resolve, reject) => {
      this.jobs.set(id, { resolve, reject, parts: [] });
      signal?.addEventListener('abort', () => this.worker.postMessage({ type: 'cancel', id }), { once: true });
      this.worker.postMessage({ type: 'generate', id, text, controls: {} });
    });
  }
}

/**
 * The on-device voices for a page. config() returns the page's /api/voices answer (its
 * `lite` section and `phonon_version`); voices are entries of config().lite.voices.
 */
export function createLite(config) {
  let phonon = null, hojo = null, tinyVoca = null;
  const workers = {};  // supertonic, pocket, kale: started on first use
  const KINDS = { supertonic: Supertonic, pocket: Pocket, kale: Kale };
  const phononVoices = new Set();

  async function phononEngine(status) {
    phonon ||= (async () => {
      const { PhononTTS } = await import(`/static/phonon-${config().phonon_version}/index.js`);
      const seen = {};
      return PhononTTS.load({ lang: 'en', model: config().lite.model, onProgress: ({ file, loaded, cached }) => {
        seen[file] = loaded || 0;
        status(statusText('phonon', seen, cached));
      } });
    })().catch(e => { phonon = null; throw e; });
    return phonon;
  }

  // Phonon's expressiveness presets are temperatures; Hojo reads best a little warmer.
  const hojoTemperature = t => {
    const preset = config().lite.expressiveness.find(e => e.temperature === t);
    return preset?.hojo_temperature ?? 0.8;
  };

  return {
    /** Speak `text` with an on-device voice: 24 kHz mono Float32Array. */
    async synth(voice, text, { temperature, seed, signal, status = () => {} }) {
      if (voice.engine === 'hojo') {
        if (!hojo) hojo = new Hojo();
        hojo.onStatus = status;
        try { await hojo.ready; } catch (e) { hojo = null; throw e; }
        return hojo.synth(text, voice.source, voice.lang, { temperature: hojoTemperature(temperature), seed, signal });
      }
      if (voice.engine === 'smoller') {
        const e = workers.smoller ||= new Smoller(voice.worker);
        e.onStatus = status;
        status('Loading the on-device voice…');
        try { await e.ready; } catch (err) { delete workers.smoller; throw err; }
        return e.synth(text, voice, { signal });
      }
      if (KINDS[voice.engine]) {
        const e = workers[voice.engine] ||= new KINDS[voice.engine]();
        e.onStatus = status;
        try { await e.ready; } catch (err) { delete workers[voice.engine]; throw err; }
        const preset = config().lite.expressiveness.find(p => p.temperature === temperature);
        return e.synth(text, voice, { temperature: preset?.pocket_temperature ?? 0.7, seed, signal });
      }
      if (voice.engine === 'tinyvoca') {
        if (!tinyVoca) tinyVoca = new TinyVoca();
        tinyVoca.onStatus = status;
        try { await tinyVoca.ready; } catch (e) { tinyVoca = null; throw e; }
        return tinyVoca.synth(text, voice.source, { signal });
      }
      const tts = await phononEngine(status);
      if (!phononVoices.has(voice.id)) { await tts.addVoice(voice.id, voice.url); phononVoices.add(voice.id); }
      return tts.synth(text, { voice: voice.id, temperature, seed, signal });
    },
  };
}
