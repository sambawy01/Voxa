// The "Advanced" panel for ZONOS3 Mini voices on the Speak page. It follows Zyphra's own studio:
// presets, an identity meter, A/B against the original voice, "voices near this one", a mood pad
// with emotions, steering sliders and accents, mood for selected words, pauses and pronunciation
// markers, blending two voices, sampling, saving a voice, and cloning with a consent step.
// Everything runs on this device; a cloned clip is kept in memory only.
import {
  applyPreset, requestFields, hasSettings, cloneProblem, toBase64, CONSENT_TEXT, CLONE_MAX_SECONDS,
  identityOf, isSameVoice, moodPoint, setMoodFromPad, chooseEmotion, nearbyVoices, blendFields, samplingFields,
  SAMPLING_DEFAULTS, collapseWithMap, toSpan,
} from '/zonos-controls.js';
import { toNpy, fromNpy } from '/zonos-studio-logic.js';

const el = (tag, attrs = {}, ...kids) => {
  const n = Object.assign(document.createElement(tag), attrs);
  for (const k of kids) n.append(k);
  return n;
};
const btn = (text, onclick, cls = 'small') => el('button', { type: 'button', className: cls, textContent: text, onclick });
const blank = () => ({ sliders: {}, accent: null, sampling: {}, blend: null, spans: [], stream: false });

/**
 * Mount the panel into `root`. Hooks from the page:
 *   loadControls(status)      Zyphra's controls document (loads the engine on first use)
 *   render(fields, text, st)  speak `text` with these extra request fields -> Blob (for previews)
 *   getText() / getSelection() / insert(before, after)   the script box
 *   previewText()             a short line in the voice's language
 *   voices()                  other ZONOS3 voices [{id, name, source}] for blending
 *   current()                 the chosen voice {id, name, source}
 *   onSave(entry) / onDelete(id)   saved voices
 * Returns { options(), signature(), cloning(), show(on), load(state), setVoice(v) }.
 */
export function mountZonosPanel(root, hooks) {
  const { loadControls, onChange = () => {} } = hooks;
  let ui = null, caps = null, state = blank();
  const clone = { file: null, bytes: null, seconds: null, on: false, rec: null };
  const sliderEls = new Map();
  let accentSel, accentStrength, presetBox, meter, meterLabel, meterFill, pad, padDot, voiceNote, spanList, saveName, deleteBtn;
  let busy = false;

  const body = el('div', { className: 'zbody' });
  const status = el('p', { className: 'hint', role: 'status' });
  const loadBtn = btn('Show the voice controls', null);
  const note = el('p', { className: 'hint', textContent: 'Steer the voice like a studio: age, accent, texture, pace, mood and more. The first time, this loads the voice model (about 1 GB, kept in your browser).' });

  const changed = () => { paint(); onChange(); };
  const setState = s => { state = { ...blank(), ...(s || {}) }; changed(); };

  // -- the player for previews (A/B, nearby voices)
  const player = el('audio', { controls: true, hidden: true });
  const playBlob = blob => { if (player.src) URL.revokeObjectURL(player.src); player.src = URL.createObjectURL(blob); player.hidden = false; player.play().catch(() => {}); };
  const sampleText = () => (hooks.getText().trim().match(/^[^.!?。！？؟]+[.!?。！？؟]?/)?.[0] || hooks.previewText()).slice(0, 160);
  async function guarded(label, fn) {
    if (busy) return;
    busy = true; status.textContent = label;
    try { await fn(); status.textContent = ''; } catch (e) { status.textContent = e.name === 'AbortError' ? '' : (e.message || 'That did not work.'); }
    busy = false;
  }
  const fieldsFor = st => ({ ...(hasSettings(st) ? requestFields(ui, st, ui.request_defaults || {}) : {}) });

  // -- top: identity, A/B, starting points, nearby voices
  function buildTop() {
    meterFill = el('i'); meterLabel = el('b');
    meter = el('div', { className: 'zmeter' }, el('div', { className: 'zbar' }, meterFill, el('span', { className: 'zmark', style: `left:${(ui.identity_threshold ?? 0.8) * 100}%` })), meterLabel);
    const ab = el('div', { className: 'zrow' },
      btn('▶ Original', () => guarded('Speaking the original voice…', async () => playBlob(await hooks.render({}, sampleText(), t => { status.textContent = t; })))),
      btn('▶ Designed', () => guarded('Speaking your design…', async () => playBlob(await hooks.render(fieldsFor(state), sampleText(), t => { status.textContent = t; })))),
      el('span', { className: 'hint', textContent: 'A/B: hear the voice before and after your changes.' }));
    presetBox = el('div', { className: 'zchips' }, ...Object.keys(ui.presets || {}).map(n => btn(n, () => setState(applyPreset(ui, n)), 'chip')));
    const near = el('div', { className: 'zchips' });
    const find = btn('Find five', () => guarded('Rendering five nearby voices…', async () => {
      near.replaceChildren();
      const options = nearbyVoices(ui, state, 5);
      for (const [i, st] of options.entries()) {
        status.textContent = `Rendering nearby voice ${i + 1} of 5…`;
        const blob = await hooks.render(fieldsFor(st), sampleText(), t => { status.textContent = t; });
        const row = el('span', { className: 'znear' }, btn(`▶ ${i + 1}`, () => playBlob(blob)), btn('Use', () => setState({ ...state, sliders: st.sliders }), 'small ghost'));
        near.append(row);
      }
    }));
    body.append(meter, ab, el('h4', { textContent: 'Starting points' }), presetBox,
      el('div', { className: 'zrow' }, el('h4', { textContent: 'Voices near this one' }), find),
      el('p', { className: 'hint', textContent: 'Five small, safe moves around this voice, rendered one after another. Play one, then use it.' }), near,
      el('div', { className: 'zrow' }, btn('Reset everything', () => setState(blank()), 'small ghost')));
  }

  // -- the mood pad and emotion chips
  function buildMood(box) {
    padDot = el('i', { className: 'zdot' });
    pad = el('div', { className: 'zpad', role: 'slider', tabIndex: 0, ariaLabel: 'Mood: calm to excited, negative to positive' },
      el('span', { className: 'zax x', textContent: 'negative ← → positive' }), el('span', { className: 'zax y', textContent: 'calm ↓ ↑ excited' }), padDot);
    const at = e => { const r = pad.getBoundingClientRect(); return [Math.max(-1, Math.min(1, ((e.clientX - r.left) / r.width) * 2 - 1)), Math.max(-1, Math.min(1, -(((e.clientY - r.top) / r.height) * 2 - 1)))]; };
    let drag = false;
    pad.onpointerdown = e => { drag = true; pad.setPointerCapture(e.pointerId); const [x, y] = at(e); state = setMoodFromPad(ui, state, x, y); changed(); };
    pad.onpointermove = e => { if (!drag) return; const [x, y] = at(e); state = setMoodFromPad(ui, state, x, y); changed(); };
    pad.onpointerup = () => { drag = false; };
    for (const m of ui.moods || []) {
      const dot = btn(m.label, () => { state = chooseEmotion(ui, state, m.id); changed(); }, 'chip zemo');
      dot.title = m.effect || '';
      dot.style.setProperty('--x', 10 + ((m.valence + 1) / 2) * 80 + '%'); dot.style.setProperty('--y', 10 + (1 - (m.arousal + 1) / 2) * 80 + '%');
      pad.append(dot);
    }
    box.append(pad);
  }

  // -- steering sliders and accents
  function buildGroups() {
    for (const g of ui.groups || []) {
      const controls = (ui.controls || []).filter(c => c.group === g.id);
      const accents = g.id === 'accent' ? (ui.accents || []) : [];
      if (!controls.length && !accents.length) continue;
      const box = el('fieldset', { className: 'zgroup' }, el('legend', { textContent: g.label }));
      if (g.id === 'mood') buildMood(box);
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
        input.oninput = () => { state.sliders[c.id] = +input.value; changed(); };
        input.title = `Safe range ${slo} to ${shi}; beyond it the voice may drift from the original.`;
        const poles = c.poles ? ` (${c.poles[0]} ↔ ${c.poles[1]})` : '';
        box.append(el('label', { className: 'zslider', htmlFor: input.id }, el('span', { textContent: c.label + poles }), val), input);
        sliderEls.set(c.id, { input, val });
      }
      body.append(box);
    }
  }

  // -- mood for the selected words
  function buildSpans() {
    const spannable = (ui.controls || []).filter(c => c.spannable && c.group !== 'who');
    const sel = el('select', { ariaLabel: 'Control for the selected words' }, ...spannable.map(c => el('option', { value: c.id, textContent: c.label })));
    const dose = el('input', { type: 'range', min: 0, max: 1, step: 0.05, value: 0.6, ariaLabel: 'Amount' });
    spanList = el('div', { className: 'zchips' });
    const add = btn('Apply to the selected words', () => {
      const raw = hooks.getSelection(), text = hooks.getText();
      if (!raw || raw.end <= raw.start) { status.textContent = 'Select some words in your script first.'; return; }
      const c = spannable.find(x => x.id === sel.value), v = +dose.value;
      const value = c.safe[1] * v;
      const { map } = collapseWithMap(text);
      const span = toSpan(ui, raw, map, { [c.id]: value });
      if (!span) return;
      state.spans = [...state.spans, { ...span, words: text.slice(raw.start, raw.end), label: c.label }];
      status.textContent = ''; changed();
    });
    body.append(el('fieldset', { className: 'zgroup' }, el('legend', { textContent: 'Words' }),
      el('p', { className: 'hint', textContent: 'Select words in your script, pick a control (angry, whisper, lively…) and apply it to just those words.' }),
      el('div', { className: 'zrow' }, sel, dose, add), spanList));
  }

  // -- pauses and pronunciation
  async function previewPronunciation(out, ph) {
    out.replaceChildren();
    const text = (hooks.getText().slice(hooks.getSelection().start, hooks.getSelection().end).trim() || hooks.getText().trim()).slice(0, 1000);
    if (!text) { status.textContent = 'Type or select some words first.'; return; }
    await guarded('Looking up pronunciations…', async () => {
      const r = await hooks.call('/v2/phonemize', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, language: hooks.current().code, guess: true }) });
      if (r.status === 404) throw new Error('This voice has no pronunciation dictionary for its language.');
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `The lookup failed (${r.status}).`);
      const { segments } = await r.json();
      for (const seg of segments) {
        if (!seg.ipa) { out.append(el('span', { className: 'zseg', textContent: seg.text })); continue; }
        const word = el('span', { className: 'zseg ' + seg.status, title: seg.status }, el('b', { textContent: seg.text }), el('i', { textContent: '/' + seg.ipa + '/' }));
        if (ph) word.append(btn('Use', () => { if (hooks.replace(seg.text, ph.open_marker + seg.ipa + ph.close_marker)) status.textContent = `Pronunciation set for "${seg.text}".`; }, 'small ghost'));
        out.append(word);
      }
    });
  }
  function buildMarkers() {
    const pauses = caps?.text_tokenizer?.pauses, ph = caps?.text_tokenizer?.phonemes;
    const box = el('fieldset', { className: 'zgroup' }, el('legend', { textContent: 'Pauses & pronunciation' }));
    const ipaOut = el('div', { className: 'zipa' });
    box.append(el('div', { className: 'zrow' }, btn('Preview pronunciation', () => previewPronunciation(ipaOut, ph)),
      el('span', { className: 'hint', textContent: 'See how each word will sound (IPA). Select words first, or it reads your whole script.' })), ipaOut);
    if (pauses?.markers?.length) {
      box.append(el('p', { className: 'hint', textContent: 'Put the cursor after a word in your script, then add a pause.' }),
        el('div', { className: 'zchips' }, ...pauses.markers.map((m, i) => btn(`${pauses.seconds?.[i] ?? ''} s pause`, () => hooks.insert(m, ''), 'chip'))));
    }
    if (ph) {
      box.append(el('p', { className: 'hint', textContent: 'Select a word and mark it to spell its sound yourself, in phonetic symbols between the markers.' }),
        btn('Mark selected word for pronunciation', () => hooks.insert(ph.open_marker, ph.close_marker), 'chip'));
    }
    body.append(box);
  }

  // -- blend two voices, sampling, save
  function buildMore() {
    const others = hooks.voices();
    if (others.length > 1) {
      const sel = el('select', { ariaLabel: 'Second voice' }, el('option', { value: '', textContent: 'No blend' }),
        ...others.map(v => el('option', { value: v.id, textContent: v.name })));
      const t = el('input', { type: 'range', min: 0, max: 1, step: 0.05, value: 0.5, ariaLabel: 'Blend position' });
      const sync = () => { state.blend = sel.value ? { id: sel.value, t: +t.value } : null; changed(); };
      sel.onchange = sync; t.oninput = sync;
      body.append(el('fieldset', { className: 'zgroup' }, el('legend', { textContent: 'Blend two voices' }),
        el('p', { className: 'hint', textContent: 'Mix this voice with another (left = this voice, right = the other). Blending replaces the steering sliders\' voice identity.' }),
        el('div', { className: 'zrow' }, sel, t)));
      blendEls = { sel, t };
    }
    const samp = el('fieldset', { className: 'zgroup' }, el('legend', { textContent: 'Sampling' }));
    const field = (key, label, min, max, step) => {
      const input = el('input', { type: 'range', min, max, step, value: SAMPLING_DEFAULTS[key], id: 'zs-' + key });
      const val = el('span', { className: 'val', textContent: String(SAMPLING_DEFAULTS[key]) });
      input.oninput = () => { state.sampling = { ...state.sampling, [key]: +input.value }; val.textContent = input.value; changed(); };
      sampEls.set(key, { input, val });
      samp.append(el('label', { className: 'zslider', htmlFor: input.id }, el('span', { textContent: label }), val), input);
    };
    field('temperature', 'Randomness (temperature)', 0.3, 1.2, 0.01);
    field('top_p', 'Variety (top-p)', 0.3, 1, 0.01);
    field('repetition_penalty', 'Repetition penalty', 1, 4, 0.1);
    field('fade_out_ms', 'Fade out at the end (ms)', 0, 500, 10);
    const laci = el('input', { type: 'checkbox', id: 'zlaci' });
    laci.onchange = () => { state.sampling = { ...state.sampling, laci: laci.checked }; changed(); };
    sampEls.set('laci', { input: laci });
    samp.append(el('label', { className: 'zconsent', htmlFor: 'zlaci' }, laci, el('span', { textContent: 'Guard against skipped or repeated words (slower)' })));
    const live = el('input', { type: 'checkbox', id: 'zlive' });
    live.onchange = () => { state.stream = live.checked; changed(); };
    liveEl = live;
    samp.append(el('label', { className: 'zconsent', htmlFor: 'zlive' }, live, el('span', { textContent: 'Start playing while it generates (you still get the finished audio)' })));
    body.append(samp);

    saveName = el('input', { type: 'text', placeholder: 'Name this voice', ariaLabel: 'Name for the saved voice', maxLength: 40 });
    deleteBtn = btn('Delete this saved voice', () => { const v = hooks.current(); if (v?.mine) hooks.onDelete(v.id); }, 'small ghost');
    deleteBtn.hidden = true;
    body.append(el('fieldset', { className: 'zgroup' }, el('legend', { textContent: 'Save this voice' }),
      el('p', { className: 'hint', textContent: 'Saved on this device, under "Your ZONOS3 voices" in the voice list. A cloned voice cannot be saved: its clip is never stored.' }),
      el('div', { className: 'zrow' }, saveName, btn('Save to Your voices', () => {
        const name = saveName.value.trim();
        if (!name) { status.textContent = 'Give the voice a name first.'; return; }
        const { spans, ...keep } = state; // eslint-disable-line no-unused-vars
        hooks.onSave({ name, state: JSON.parse(JSON.stringify({ ...keep, spans: [] })) });
        status.textContent = `Saved "${name}".`;
      }), btn('Add to cast', () => {
        const name = saveName.value.trim() || hooks.current().name;
        const { spans, ...keep } = state; // eslint-disable-line no-unused-vars
        hooks.onCast({ name, state: JSON.parse(JSON.stringify({ ...keep, spans: [] })) });
        status.textContent = `Added "${name}" to the cast. Open the Studio to write a scene.`;
      }, 'small ghost')), el('p', { className: 'hint' }, el('a', { href: '/studio', textContent: 'Open the Studio: edit speech, direct a cast →' })), deleteBtn));
  }
  let blendEls = null, liveEl = null;
  const sampEls = new Map();

  function build() {
    body.replaceChildren(); sliderEls.clear(); sampEls.clear(); blendEls = null;
    buildTop(); buildGroups(); buildSpans(); buildMarkers(); buildMore();
    paint();
  }

  function paint() {
    if (!ui) return;
    for (const [id, { input, val }] of sliderEls) { input.value = state.sliders[id] ?? 0; val.textContent = (+input.value).toFixed(1); }
    if (accentSel) { accentSel.value = state.accent?.id || ''; accentStrength.value = state.accent?.strength ?? 1; }
    const id = identityOf(ui, state);
    meterFill.style.width = Math.round(id * 100) + '%';
    meterLabel.textContent = isSameVoice(ui, state) ? 'Still the same voice' : 'A new person';
    meter.classList.toggle('new', !isSameVoice(ui, state));
    const p = moodPoint(ui, state);
    padDot.style.left = ((p.x + 1) / 2) * 100 + '%'; padDot.style.top = (1 - (p.y + 1) / 2) * 100 + '%';
    for (const [k, { input, val }] of sampEls) {
      if (k === 'laci') input.checked = !!state.sampling?.laci;
      else { input.value = state.sampling?.[k] ?? SAMPLING_DEFAULTS[k]; val.textContent = input.value; }
    }
    if (liveEl) liveEl.checked = !!state.stream;
    if (blendEls) { blendEls.sel.value = state.blend?.id || ''; blendEls.t.value = state.blend?.t ?? 0.5; }
    spanList.replaceChildren(...state.spans.map((s, i) => btn(`“${s.words.slice(0, 24)}” · ${s.label} ✕`, () => { state.spans = state.spans.filter((_, j) => j !== i); changed(); }, 'chip')));
    const v = hooks.current();
    deleteBtn.hidden = !v?.mine;
  }

  async function ensure(report = t => { status.textContent = t; }) {
    if (ui) return;
    const doc = await loadControls(report);
    ui = doc.ui; caps = doc.capabilities || null;
    build(); loadBtn.hidden = true; note.hidden = true;
  }
  loadBtn.onclick = async () => {
    loadBtn.disabled = true; status.textContent = 'Loading the voice controls…';
    try { await ensure(); status.textContent = ''; }
    catch (e) { status.textContent = e.message || 'Could not load the controls.'; loadBtn.disabled = false; }
  };

  // -- cloning
  const consent = el('input', { type: 'checkbox', id: 'zconsent' });
  const consentLabel = el('label', { htmlFor: 'zconsent', className: 'zconsent' }, consent, el('span', { textContent: CONSENT_TEXT }));
  const fileIn = el('input', { type: 'file', accept: 'audio/*,video/mp4,video/webm', ariaLabel: 'Voice sample' });
  const recBtn = btn('● Record', null);
  const useRow = el('label', { className: 'zconsent' });
  const useBox = el('input', { type: 'checkbox', disabled: true });
  const cloneNote = el('p', { className: 'hint', role: 'status' });
  const preview = el('audio', { controls: true, hidden: true });
  const clearBtn = btn('Remove the clip', null, 'small ghost'); clearBtn.hidden = true;
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

  // -- speaker files (.npy): save a voice's fingerprint, or load one. Same consent as cloning.
  const emb = { b64: null, name: '', on: false };
  const embIn = el('input', { type: 'file', accept: '.npy', ariaLabel: 'Speaker file' });
  const embUse = el('input', { type: 'checkbox', disabled: true });
  const embNote = el('p', { className: 'hint', role: 'status' });
  const embRow = el('label', { className: 'zconsent' }, embUse, el('span', { textContent: 'Speak with this speaker file instead of the voice above' }));
  const download = (bytes, name) => {
    const a = el('a', { href: URL.createObjectURL(new Blob([bytes], { type: 'application/octet-stream' })), download: name });
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  };
  const SESSION = { 'Content-Type': 'application/json', 'X-TTS-Session-ID': 'voxa' };
  const saveFile = btn('Save the speaker file (.npy)', () => guarded('Reading the voice…', async () => {
    const cur = hooks.current();
    let id = cur.source, label = cur.name;
    if (clone.on && clone.bytes) {
      const problem = cloneProblem({ consent: consent.checked, file: clone.file, seconds: clone.seconds });
      if (problem) throw new Error(problem);
      const r = await hooks.call('/v2/speakers', { method: 'POST', headers: SESSION, body: JSON.stringify({ label: 'clone', speaker_audio_base64: toBase64(clone.bytes), speaker_audio_name: 'clip' }) });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `The voice could not be read (${r.status}).`);
      id = (await r.json()).id; label = 'my-clone';
    }
    const e = await hooks.call(`/v2/speakers/${encodeURIComponent(id)}/embedding`, { headers: { 'X-TTS-Session-ID': 'voxa' } });
    if (!e.ok) throw new Error((await e.json().catch(() => ({}))).detail || `The speaker file could not be made (${e.status}).`);
    download(toNpy((await e.json()).embedding), `${label.replace(/[^\w.-]+/g, '-')}.npy`);
  }), 'small');
  embIn.onchange = async () => {
    emb.b64 = null; emb.on = false; embUse.checked = false; embUse.disabled = true;
    const f = embIn.files[0];
    if (!f) { embNote.textContent = ''; changed(); return; }
    try {
      const bytes = new Uint8Array(await f.arrayBuffer());
      fromNpy(bytes); // checks that it is a speaker vector
      emb.b64 = toBase64(bytes); emb.name = f.name;
      embNote.textContent = `${f.name}: a speaker file. Confirm permission above to use it.`;
      embUse.disabled = !consent.checked;
    } catch (e) { embNote.textContent = e.message; }
    changed();
  };
  embUse.onchange = () => { emb.on = embUse.checked; changed(); };
  const syncEmb = () => { embUse.disabled = !(emb.b64 && consent.checked); if (embUse.disabled) { embUse.checked = false; emb.on = false; } };
  consent.addEventListener('change', syncEmb);

  const cloneBox = el('fieldset', { className: 'zgroup' }, el('legend', { textContent: 'Clone a voice' }),
    el('p', { className: 'hint', textContent: 'Give it a short clip and it speaks in that voice. The clip stays on this device: it is never uploaded, and it is forgotten when you close the page.' }),
    consentLabel, el('div', { className: 'zrow' }, fileIn, recBtn), cloneNote, preview, clearBtn, useRow,
    el('h4', { textContent: 'Speaker files' }),
    el('p', { className: 'hint', textContent: 'A speaker file (.npy) is a small fingerprint of a voice. Save one to reuse it later without the clip, or load one you saved. It needs the same permission as cloning, and is never stored by Voxa.' }),
    el('div', { className: 'zrow' }, saveFile, embIn), embNote, embRow);

  const details = el('details', { className: 'zadv' }, el('summary', { textContent: 'Advanced: design the voice, or clone one' }),
    note, loadBtn, status, body, player, cloneBox);
  root.append(details);

  function options() {
    let fields = {}, spans;
    if (ui) {
      fields = fieldsFor(state);
      Object.assign(fields, samplingFields(state.sampling));
      const cur = hooks.current();
      if (state.blend && cur) Object.assign(fields, blendFields(cur.source, hooks.voices().find(v => v.id === state.blend.id)?.source, state.blend.t));
      // Spans only apply while the words they were marked on are still where they were.
      const { text } = collapseWithMap(hooks.getText());
      const live = state.spans.filter(s => text.slice(s.start, s.end) === collapseWithMap(s.words).text);
      if (live.length) spans = live.map(({ start, end, controls }) => ({ start, end, controls }));
    }
    if (emb.on && emb.b64 && consent.checked) fields.speaker_embedding_base64 = emb.b64;
    else if (clone.on && clone.bytes && !cloneProblem({ consent: consent.checked, file: clone.file, seconds: clone.seconds })) {
      fields.speaker_audio_base64 = toBase64(clone.bytes);
    }
    return Object.keys(fields).length || spans ? { fields, spans, stream: !!state.stream } : undefined;
  }
  return {
    options,
    /** A short text for cache keys: what is set, without the clip's bytes. */
    signature() {
      const o = options();
      if (!o) return '';
      const f = { ...o.fields, speaker_audio_base64: o.fields.speaker_audio_base64 ? clone.file?.size + ':' + clone.file?.lastModified : undefined, speaker_embedding_base64: o.fields.speaker_embedding_base64 ? emb.name + ':' + emb.b64.length : undefined };
      return JSON.stringify({ f, s: o.spans });
    },
    cloning: () => !!((clone.on && clone.bytes) || emb.on),
    /** Load the controls document now (a saved voice needs it to apply its settings). */
    ensure,
    show(on) { root.hidden = !on; },
    /** Load a saved voice's settings (or clear them). */
    load(s) { state = { ...blank(), ...(s || {}), spans: [] }; if (ui) paint(); },
    setVoice() { if (ui) paint(); },
  };
}
