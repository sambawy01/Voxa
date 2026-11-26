// The "Advanced" panel for ZONOS3 Mini voices on the Speak page: steering sliders (who, accent,
// texture, room, delivery, mood), presets, and voice cloning with a consent step. Everything runs
// on this device; a cloned clip is kept in memory only and is gone when the page closes.
import { applyPreset, requestFields, hasSettings, cloneProblem, toBase64, CONSENT_TEXT, CLONE_MAX_SECONDS } from '/zonos-controls.js';

const el = (tag, attrs = {}, ...kids) => {
  const n = Object.assign(document.createElement(tag), attrs);
  for (const k of kids) n.append(k);
  return n;
};

/**
 * Mount the panel into `root`. `loadControls()` returns Zyphra's controls document (it loads the
 * engine on first use). Returns `{ options(), show(on) }`; options() is what to pass as `zonos`
 * to LITE.synth, or `undefined` when nothing is set.
 */
export function mountZonosPanel(root, { loadControls, onChange = () => {} }) {
  let ui = null, state = { sliders: {}, accent: null };
  const clone = { file: null, bytes: null, seconds: null, name: '', on: false, rec: null };
  const body = el('div', { className: 'zbody' });
  const sliderEls = new Map();
  let accentSel, accentStrength, presetSel;

  const status = el('p', { className: 'hint', role: 'status' });
  const loadBtn = el('button', { type: 'button', className: 'small', textContent: 'Show the voice controls' });
  const note = el('p', { className: 'hint', textContent: 'Steer age, accent, texture, pace and mood. The first time, this loads the voice model (about 1 GB, kept in your browser).' });

  // -- the steering controls
  function buildControls() {
    body.replaceChildren();
    presetSel = el('select', { ariaLabel: 'Preset' }, el('option', { value: '', textContent: 'Presets…' }),
      ...Object.keys(ui.presets || {}).map(n => el('option', { value: n, textContent: n })));
    presetSel.onchange = () => { if (presetSel.value) { setState(applyPreset(ui, presetSel.value)); } };
    const reset = el('button', { type: 'button', className: 'small ghost', textContent: 'Reset' });
    reset.onclick = () => { presetSel.value = ''; setState({ sliders: {}, accent: null }); };
    body.append(el('div', { className: 'zrow' }, presetSel, reset));

    for (const g of ui.groups || []) {
      const controls = (ui.controls || []).filter(c => c.group === g.id);
      const accents = g.id === 'accent' ? (ui.accents || []) : [];
      if (!controls.length && !accents.length) continue;
      const box = el('fieldset', { className: 'zgroup' }, el('legend', { textContent: g.label }));
      if (accents.length) {
        accentSel = el('select', { ariaLabel: 'Accent' }, el('option', { value: '', textContent: 'No accent change' }),
          ...accents.map(a => el('option', { value: a.id, textContent: a.label })));
        accentStrength = el('input', { type: 'range', min: 0, max: 2, step: 0.1, value: 1, ariaLabel: 'Accent strength' });
        const sync = () => { state.accent = accentSel.value ? { id: accentSel.value, strength: +accentStrength.value } : null; changed(); };
        accentSel.onchange = sync; accentStrength.oninput = sync;
        box.append(el('div', { className: 'zrow' }, accentSel, accentStrength));
      }
      for (const c of controls) {
        const [lo, hi] = c.range, [slo, shi] = c.safe || c.range;
        const input = el('input', { type: 'range', min: lo, max: hi, step: 0.1, value: 0, id: 'zc-' + c.id });
        const val = el('span', { className: 'val', textContent: '0' });
        input.oninput = () => { state.sliders[c.id] = +input.value; val.textContent = (+input.value).toFixed(1); presetSel.value = ''; changed(); };
        input.title = `Safe range ${slo} to ${shi}; beyond it the voice may drift from the original.`;
        const poles = c.poles ? ` (${c.poles[0]} ↔ ${c.poles[1]})` : '';
        box.append(el('label', { className: 'zslider', htmlFor: input.id }, el('span', { textContent: c.label + poles }), val), input);
        sliderEls.set(c.id, { input, val });
      }
      body.append(box);
    }
    paint();
  }
  function setState(s) {
    state = s || { sliders: {}, accent: null };
    paint(); changed();
  }
  function paint() {
    for (const [id, { input, val }] of sliderEls) { input.value = state.sliders[id] ?? 0; val.textContent = (+input.value).toFixed(1); }
    if (accentSel) { accentSel.value = state.accent?.id || ''; accentStrength.value = state.accent?.strength ?? 1; }
  }
  const changed = () => onChange(options());

  loadBtn.onclick = async () => {
    loadBtn.disabled = true; status.textContent = 'Loading the voice controls…';
    try { ui = (await loadControls(t => { status.textContent = t; })).ui; buildControls(); status.textContent = ''; loadBtn.hidden = true; note.hidden = true; }
    catch (e) { status.textContent = e.message || 'Could not load the controls.'; loadBtn.disabled = false; }
  };

  // -- cloning
  const consent = el('input', { type: 'checkbox', id: 'zconsent' });
  const consentLabel = el('label', { htmlFor: 'zconsent', className: 'zconsent' }, consent, el('span', { textContent: CONSENT_TEXT }));
  const fileIn = el('input', { type: 'file', accept: 'audio/*,video/mp4,video/webm', ariaLabel: 'Voice sample' });
  const recBtn = el('button', { type: 'button', className: 'small', textContent: '● Record' });
  const useRow = el('label', { className: 'zconsent' });
  const useBox = el('input', { type: 'checkbox', disabled: true });
  const cloneNote = el('p', { className: 'hint', role: 'status' });
  const preview = el('audio', { controls: true, hidden: true });
  const clearBtn = el('button', { type: 'button', className: 'small ghost', textContent: 'Remove the clip', hidden: true });
  useRow.append(useBox, el('span', { textContent: 'Speak with this voice instead of the one above' }));

  async function setClip(file) {
    clone.file = file; clone.on = false; useBox.checked = false; useBox.disabled = true; preview.hidden = true; clearBtn.hidden = true;
    clone.bytes = null; clone.seconds = null;
    if (!file) { cloneNote.textContent = ''; changed(); return; }
    try {
      clone.bytes = new Uint8Array(await file.arrayBuffer());
      const buf = await new OfflineAudioContext(1, 1, 44100).decodeAudioData(clone.bytes.slice().buffer);
      clone.seconds = buf.duration;
    } catch { clone.file = null; clone.bytes = null; cloneNote.textContent = 'That file could not be read as audio.'; changed(); return; }
    preview.src = URL.createObjectURL(file); preview.hidden = false; clearBtn.hidden = false;
    const long = clone.seconds > CLONE_MAX_SECONDS ? ` Only the first ${CLONE_MAX_SECONDS} seconds are used.` : '';
    cloneNote.textContent = `${file.name || 'Recording'}: ${clone.seconds.toFixed(1)} seconds.${long} Clear speech from one person, with little background noise, works best.`;
    refreshUse(); changed();
  }
  function refreshUse() {
    const problem = cloneProblem({ consent: consent.checked, file: clone.file, seconds: clone.seconds });
    useBox.disabled = !!problem;
    if (problem) { useBox.checked = false; clone.on = false; }
    if (clone.file && problem) cloneNote.textContent = problem;
  }
  consent.onchange = () => { refreshUse(); changed(); };
  useBox.onchange = () => { clone.on = useBox.checked; changed(); };
  fileIn.onchange = () => setClip(fileIn.files[0] || null);
  clearBtn.onclick = () => { fileIn.value = ''; setClip(null); };
  recBtn.onclick = async () => {
    if (clone.rec) { clone.rec.stop(); return; }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const chunks = [], rec = new MediaRecorder(stream);
      clone.rec = rec; recBtn.textContent = '■ Stop'; cloneNote.textContent = 'Recording… speak naturally for 10 to 30 seconds.';
      const timer = setTimeout(() => rec.state === 'recording' && rec.stop(), CLONE_MAX_SECONDS * 1000);
      rec.ondataavailable = e => e.data.size && chunks.push(e.data);
      rec.onstop = () => {
        clearTimeout(timer); stream.getTracks().forEach(t => t.stop()); clone.rec = null; recBtn.textContent = '● Record';
        setClip(new File(chunks, 'Recording', { type: rec.mimeType || 'audio/webm' }));
      };
      rec.start();
    } catch { cloneNote.textContent = 'The microphone is not available. You can upload a file instead.'; }
  };

  const cloneBox = el('fieldset', { className: 'zgroup' }, el('legend', { textContent: 'Clone a voice' }),
    el('p', { className: 'hint', textContent: 'Give it a short clip and it speaks in that voice. The clip stays on this device: it is never uploaded, and it is forgotten when you close the page.' }),
    consentLabel, el('div', { className: 'zrow' }, fileIn, recBtn), cloneNote, preview, clearBtn, useRow);

  const details = el('details', { className: 'zadv' }, el('summary', { textContent: 'Advanced: steer the voice, or clone one' }),
    note, loadBtn, status, body, cloneBox);
  root.append(details);

  function options() {
    const fields = ui && hasSettings(state) ? requestFields(ui, state, ui.request_defaults || {}) : {};
    if (clone.on && clone.bytes) {
      const problem = cloneProblem({ consent: consent.checked, file: clone.file, seconds: clone.seconds });
      if (!problem) fields.speaker_audio_base64 = toBase64(clone.bytes);
    }
    return Object.keys(fields).length ? fields : undefined;
  }
  return {
    options,
    /** A short text for cache keys: what is set, without the clip's bytes. */
    signature() { const o = options() || {}; return JSON.stringify({ ...o, speaker_audio_base64: o.speaker_audio_base64 ? clone.file?.size + ':' + clone.file?.lastModified : undefined }); },
    cloning: () => !!(clone.on && clone.bytes),
    show(on) { root.hidden = !on; },
  };
}
