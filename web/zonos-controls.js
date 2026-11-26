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
