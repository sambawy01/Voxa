// ZONOS3 Mini's "Advanced" settings: how the sliders become a /v2/generate request.
//
// Zyphra's steering bundle describes its own controls (GET /v2/controls -> `ui`): each control has a
// type (bipolar or unipolar), a range, a "safe" range and a `map` from its pole to the steering
// directions it moves ("<set>.<direction>" -> dose per unit). Accents are listed separately and
// presets name controls and accents ("accent:british"). Nothing here hard-codes a control: the
// panel and the request are built from that document, so a newer bundle just works.

/** Sum the doses of every control the user moved into one `controls` object for the request. */
export function buildControls(ui, state) {
  const out = {};
  const add = (map, v) => { for (const [dir, dose] of Object.entries(map || {})) out[dir] = (out[dir] || 0) + dose * v; };
  const byId = Object.fromEntries((ui.controls || []).map(c => [c.id, c]));
  for (const [id, raw] of Object.entries(state.sliders || {})) {
    const c = byId[id], v = Number(raw);
    if (!c || !Number.isFinite(v) || v === 0) continue;
    const [lo, hi] = c.range;
    const x = Math.min(hi, Math.max(lo, v));
    add(x > 0 ? c.map['+'] : c.map['-'], Math.abs(x));
  }
  if (state.accent && state.accent.id) {
    const a = (ui.accents || []).find(x => x.id === state.accent.id);
    const s = Math.min(a?.range?.[1] ?? 2, Math.max(0, Number(state.accent.strength) || 0));
    if (a && s > 0) add(a.map, s);
  }
  for (const k of Object.keys(out)) if (!Number.isFinite(out[k]) || out[k] === 0) delete out[k];
  return out;
}

/** A preset ("BBC newsreader") as slider values and an accent. */
export function applyPreset(ui, name) {
  const p = (ui.presets || {})[name];
  if (!p) return null;
  const state = { sliders: {}, accent: null };
  for (const [k, v] of Object.entries(p)) {
    if (k.startsWith('accent:')) state.accent = { id: k.slice(7), strength: v };
    else state.sliders[k] = v;
  }
  return state;
}

/** Is anything moved from its default? */
export const hasSettings = state =>
  Object.values(state.sliders || {}).some(v => Number(v) !== 0) || !!(state.accent && state.accent.strength > 0);

/** The extra fields of a /v2/generate request for these settings (empty when nothing is set). */
export function requestFields(ui, state, defaults = {}) {
  const controls = buildControls(ui, state);
  return Object.keys(controls).length ? { ...defaults, controls } : {};
}

// -- voice cloning ---------------------------------------------------------------------------

/** The reference clip may be at most this long; the engine keeps the first part of a longer one. */
export const CLONE_MAX_SECONDS = 60;
export const CLONE_MIN_SECONDS = 3;
export const CLONE_MAX_BYTES = 30 * 1024 * 1024;

export const CONSENT_TEXT = 'This is my own voice, or the speaker gave me permission to clone it. ' +
  'I will not use it to impersonate anyone or to mislead people, and I will say the speech is AI-generated when I share it.';

/** Cloning is only allowed once consent is ticked and a usable clip is loaded. Returns an error text or ''. */
export function cloneProblem({ consent, file, seconds }) {
  if (!file) return 'Choose or record a clip of the voice first.';
  if (!consent) return 'Please confirm you have the speaker\'s permission.';
  if (file.size > CLONE_MAX_BYTES) return 'That file is too large: use a clip under 30 MB.';
  if (seconds != null && seconds < CLONE_MIN_SECONDS) return `That clip is too short: use at least ${CLONE_MIN_SECONDS} seconds of clear speech.`;
  return '';
}

/** Bytes to base64 (the engine takes the clip as base64, and decodes at most 60 s of it). */
export function toBase64(bytes) {
  let s = '';
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

// -- the studio features: identity, mood pad, nearby voices, blends, spans, sampling --------------

/** Piecewise-linear value of a curve [[x, y], ...] at x (clamped at both ends). */
export function interp(curve, x) {
  if (!curve || !curve.length) return 1;
  if (x <= curve[0][0]) return curve[0][1];
  for (let i = 1; i < curve.length; i++) {
    if (x <= curve[i][0]) {
      const [x0, y0] = curve[i - 1], [x1, y1] = curve[i];
      return y0 + (y1 - y0) * (x - x0) / (x1 - x0);
    }
  }
  return curve[curve.length - 1][1];
}

/**
 * How close the voice still is to the original, 0..1: the lowest of the identity curves of the
 * controls that were moved (Zyphra measured these). Below ui.identity_threshold it is "a new person".
 */
export function identityOf(ui, state) {
  let low = 1;
  const byId = Object.fromEntries((ui.controls || []).map(c => [c.id, c]));
  for (const [id, v] of Object.entries(state.sliders || {})) {
    const c = byId[id];
    if (c && c.identity && Number(v) !== 0) low = Math.min(low, interp(c.identity, Math.abs(Number(v))));
  }
  const a = state.accent && (ui.accents || []).find(x => x.id === state.accent.id);
  if (a && a.identity && state.accent.strength > 0) low = Math.min(low, interp(a.identity, state.accent.strength));
  return low;
}
export const isSameVoice = (ui, state) => identityOf(ui, state) >= (ui.identity_threshold ?? 0.8);

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/** Where the mood sits on the pad: x = valence, y = arousal, each -1..1 of the control's range. */
export function moodPoint(ui, state) {
  const byId = Object.fromEntries((ui.controls || []).map(c => [c.id, c]));
  const axis = id => { const c = byId[id], v = Number(state.sliders?.[id]) || 0; return c ? clamp(v / Math.max(Math.abs(c.range[0]), Math.abs(c.range[1])), -1, 1) : 0; };
  return { x: axis('valence'), y: axis('arousal') };
}

/** Drag on the pad: sets valence and arousal and clears the single-emotion sliders. */
export function setMoodFromPad(ui, state, x, y) {
  const byId = Object.fromEntries((ui.controls || []).map(c => [c.id, c]));
  const next = { ...state, sliders: { ...state.sliders } };
  for (const m of ui.moods || []) delete next.sliders[m.control];
  const scale = id => { const c = byId[id]; return c ? Math.round(clamp(id === 'valence' ? x : y, -1, 1) * Math.min(Math.abs(c.safe[0]), Math.abs(c.safe[1])) * 10) / 10 : 0; };
  next.sliders.valence = scale('valence');
  next.sliders.arousal = scale('arousal');
  return next;
}

/** Tap an emotion (Happy, Sad...): its own slider at Zyphra's recommended dose, the pad axes cleared. */
export function chooseEmotion(ui, state, moodId) {
  const m = (ui.moods || []).find(x => x.id === moodId);
  if (!m) return state;
  const next = { ...state, sliders: { ...state.sliders, valence: 0, arousal: 0 } };
  for (const o of ui.moods) delete next.sliders[o.control];
  next.sliders[m.control] = m.dose;
  return next;
}

const NEARBY = ['gender', 'age', 'pitch', 'range', 'deep', 'bright', 'husky', 'raspy', 'soft', 'authority', 'chatty', 'lively', 'pace'];

/** `n` small, safe moves around the current settings: two or three controls nudged within a third of their safe range. */
export function nearbyVoices(ui, state, n = 5, rng = Math.random) {
  const byId = Object.fromEntries((ui.controls || []).map(c => [c.id, c]));
  const ids = NEARBY.filter(id => byId[id]);
  const out = [];
  for (let k = 0; k < n; k++) {
    const next = { ...state, sliders: { ...state.sliders } };
    const picks = [...ids].sort(() => rng() - 0.5).slice(0, 2 + Math.floor(rng() * 2));
    for (const id of picks) {
      const c = byId[id], [slo, shi] = c.safe, span = (shi - slo) / 3;
      const cur = Number(next.sliders[id]) || 0;
      next.sliders[id] = Math.round(clamp(cur + (rng() * 2 - 1) * span, slo, shi) * 10) / 10;
    }
    out.push(next);
  }
  return out;
}

/** Blend two built-in voices: speaker ids and a position (0 = a, 1 = b). */
export function blendFields(idA, idB, t) {
  return idA && idB && idA !== idB ? { speaker_blend_embedding_id_a: idA, speaker_blend_embedding_id_b: idB, speaker_blend_t: clamp(Number(t) || 0, 0, 1) } : {};
}

/** Only the sampling settings that differ from Zyphra's defaults. */
export const SAMPLING_DEFAULTS = { temperature: 0.75, top_p: 0.69, topk: 80, repetition_penalty: 2.4, fade_out_ms: 0 };
export function samplingFields(s = {}) {
  const out = {};
  for (const [k, d] of Object.entries(SAMPLING_DEFAULTS)) if (s[k] != null && Number(s[k]) !== d) out[k] = Number(s[k]);
  if (s.laci) out.laci = true;
  return out;
}

/** Whitespace collapsed to single spaces, with a map from each raw index to its index in the result. */
export function collapseWithMap(text) {
  let out = '', prevSpace = true;
  const map = new Array(text.length + 1);
  for (let i = 0; i < text.length; i++) {
    map[i] = out.length;
    if (/\s/.test(text[i])) { if (!prevSpace) { out += ' '; prevSpace = true; } } else { out += text[i]; prevSpace = false; }
  }
  map[text.length] = out.length;
  const trimmed = out.trimEnd(), lead = trimmed.length - trimmed.trimStart().length;
  return { text: trimmed.trimStart(), map: map.map(i => clamp(i - lead, 0, trimmed.trimStart().length)) };
}

/** Sentence-sized chunks of at most `max` characters, each with its start offset in `text`. */
export function chunkText(text, max = 300) {
  const parts = [...text.matchAll(/[^.!?。！？؟।]+[.!?。！？؟।]*["'”’)\]]*\s*/g)].map(m => ({ s: m[0], at: m.index }));
  if (!parts.length) return [{ text, at: 0 }];
  const chunks = [];
  let buf = null;
  for (const p of parts) {
    if (buf && (buf.s + p.s).length > max) { chunks.push(buf); buf = { s: p.s, at: p.at }; }
    else buf = buf ? { s: buf.s + p.s, at: buf.at } : { s: p.s, at: p.at };
  }
  if (buf) chunks.push(buf);
  return chunks.map(c => { const t = c.s.trim(); return { text: t, at: c.at + (c.s.length - c.s.trimStart().length) }; }).filter(c => c.text);
}

/** control_spans for one chunk: the spans that overlap it, shifted to the chunk's own characters. */
export function spansForChunk(spans, chunk) {
  const end = chunk.at + chunk.text.length;
  return (spans || []).filter(s => s.end > chunk.at && s.start < end)
    .map(s => ({ start: Math.max(s.start, chunk.at) - chunk.at, end: Math.min(s.end, end) - chunk.at, controls: s.controls }));
}

/** A span the user marked (raw text offsets + the sliders for it) as request spans (collapsed offsets + doses). */
export function toSpan(ui, raw, map, sliders) {
  const controls = buildControls(ui, { sliders });
  if (!Object.keys(controls).length || raw.end <= raw.start) return null;
  return { start: map[raw.start], end: map[raw.end], controls };
}

/**
 * Speaker files and voice blends only work when the request says speaker_mode "embedding": without it the engine
 * uses the checkpoint's default (raw reference audio) and answers 400 "does not accept speaker embedding files or
 * embedding blends". Found by running the real engine.
 */
export function withEmbeddingMode(fields) {
  const needs = fields.speaker_embedding_base64 || fields.speaker_blend_embedding_id_a || fields.speaker_blend_embedding_id_b;
  return needs && !fields.speaker_mode ? { ...fields, speaker_mode: 'embedding' } : fields;
}
