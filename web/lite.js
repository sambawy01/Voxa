// On-device voices, shared by every page: two engines behind one call.
//   Phonon 31m (Gradium) for its English voices: ~65 MB, loaded from Hugging Face.
//   Hojo TTS Light 40M (HojoAI) for Voxa's other English voices and Chinese: ~126 MB, served
//   from static/ and run in web/hojo-worker.js.
// Each engine loads the first time one of its voices speaks, then comes from the cache.
import { normalizeForHojo, splitForHojo } from '/hojo-text.js';

const ABOUT_MB = { phonon: 65, hojo: 126 };

function statusText(engine, files, cached) {
  const mb = Object.values(files).reduce((a, b) => a + b, 0) / 1e6;
  return cached ? 'Loading the on-device voice…'
    : `Downloading the on-device voice (once)… ${mb.toFixed(0)} MB of about ${ABOUT_MB[engine]}`;
}

class Hojo {
  constructor() {
    this.worker = new Worker('/hojo-worker.js', { type: 'module' });
    this.jobs = new Map();
    this.next = 1;
    this.onStatus = () => {};
    this.ready = new Promise((resolve, reject) => { this.loaded = { resolve, reject }; });
    this.worker.onmessage = ({ data }) => {
      if (data.type === 'progress') return this.onStatus(statusText('hojo', { all: data.total }, data.cached));
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

/**
 * The on-device voices for a page. config() returns the page's /api/voices answer (its
 * `lite` section and `phonon_version`); voices are entries of config().lite.voices.
 */
export function createLite(config) {
  let phonon = null, hojo = null;
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
      const tts = await phononEngine(status);
      if (!phononVoices.has(voice.id)) { await tts.addVoice(voice.id, voice.url); phononVoices.add(voice.id); }
      return tts.synth(text, { voice: voice.id, temperature, seed, signal });
    },
  };
}
