// The Studio page: speech edit and cast scenes, on ZONOS3 Mini, on this device.
import { requestFields, samplingFields, toBase64 } from '/zonos-controls.js';
import { diffWords, changedCount, parseScript, makeProject, readProject, concat } from '/zonos-studio-logic.js';
import { CAST_KEY, PROJECTS_KEY, loadList, saveList, registerMine } from '/zonos-voices.js';

const $ = s => document.querySelector(s);
const B = document.body.dataset;
const EDIT_MAX_SECONDS = 150;
const errText = e => (e && e.message) || String(e);

let LITE = null, VOICES = {}, ENGINES = null, ENC = null, UI = null;

// -- start up: voices, engine wrapper, gate for phones and browsers without WebGPU
async function start() {
  const [langs, v, lite, book] = await Promise.all([
    fetch('/api/languages').then(r => r.json()), fetch('/api/voices').then(r => r.json()),
    import(`/lite.js?v=${B.lite}`), import(`/book.js?v=${B.book}`),
  ]);
  LITE = v.lite; ENC = book;
  const phone = /Android|iPhone|iPad|Mobile/i.test(navigator.userAgent);
  const gate = msg => { $('#gate').hidden = false; $('#gatemsg').textContent = msg; $('#app').hidden = true; };
  if (!LITE) return gate('This Voxa server has no on-device voices.');
  if (phone || !navigator.gpu) return gate('Studio needs a desktop or laptop computer with WebGPU (recent Chrome, Edge or Safari). Your browser or device cannot run it.');
  const zonos = Object.values(LITE.voices).flat().filter(x => x.engine === 'zonos');
  if (!zonos.length) return gate('ZONOS3 Mini is not available on this Voxa server.');
  for (const x of zonos) VOICES[x.id] = x;
  registerMine(VOICES);
  ENGINES = lite.createLite(() => ({ lite: LITE, phonon_version: v.phonon_version || '' }));
  fillVoiceSelects(langs.languages || {});
  renderCast(); renderProjects();
}

function fillVoiceSelects(LANG) {
  const groups = {};
  for (const x of Object.values(VOICES)) (groups[x.lang] ||= []).push(x);
  const html = Object.entries(groups).map(([k, l]) =>
    `<optgroup label="${LANG[k] || k}">${l.map(x => `<option value="${x.id}">${x.name}</option>`).join('')}</optgroup>`).join('');
  $('#cVoice').innerHTML = html;
  const sel = $('#eVoice');
  // Editing needs a language, not a voice: one entry per language.
  sel.innerHTML = Object.entries(groups).map(([k, l]) => `<option value="${l[0].id}">${LANG[k] || k}</option>`).join('');
  const en = Object.entries(groups).find(([k]) => k === 'a');
  if (en) sel.value = en[1][0].id;
}
const call = (voice, path, init, status) => ENGINES.zonosCall(voice, path, init, status);
const JSONH = { 'Content-Type': 'application/json' };
async function detail(r, fallback) { return (await r.json().catch(() => ({}))).detail || fallback; }

// -- tabs
for (const [tab, panel] of [['#tabEdit', '#pEdit'], ['#tabCast', '#pCast']]) {
  $(tab).onclick = () => {
    for (const [t, p] of [['#tabEdit', '#pEdit'], ['#tabCast', '#pCast']]) { $(t).setAttribute('aria-selected', t === tab); $(p).hidden = p !== panel; }
  };
}

// ============================================================== speech edit
$('#eConsentText').textContent = 'This is my own recording, or the speaker gave me permission to edit it. ' +
  'I will not use it to put words in someone\'s mouth or to mislead people, and I will say the speech is AI-edited when I share it.';
const E = { file: null, bytes: null, seconds: 0, rec: null, abort: null };
const eVoice = () => VOICES[$('#eVoice').value];

async function setEditClip(file) {
  E.file = file; E.bytes = null; E.seconds = 0; $('#ePlay').hidden = true; $('#eErr').textContent = '';
  if (!file) { $('#eNote').textContent = ''; return; }
  try {
    E.bytes = new Uint8Array(await file.arrayBuffer());
    const buf = await new OfflineAudioContext(1, 1, 44100).decodeAudioData(E.bytes.slice().buffer);
    E.seconds = buf.duration;
  } catch { E.file = null; E.bytes = null; $('#eNote').textContent = 'That file could not be read as audio.'; return; }
  $('#ePlay').src = URL.createObjectURL(file); $('#ePlay').hidden = false;
  $('#eNote').textContent = E.seconds > EDIT_MAX_SECONDS
    ? `${E.seconds.toFixed(1)} seconds: too long. A recording can be at most ${EDIT_MAX_SECONDS} seconds; trim it first.`
    : `${file.name || 'Recording'}: ${E.seconds.toFixed(1)} seconds.`;
}
$('#eFile').onchange = e => setEditClip(e.target.files[0] || null);
$('#eRec').onclick = async () => {
  if (E.rec) { E.rec.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const chunks = [], rec = new MediaRecorder(stream);
    E.rec = rec; $('#eRec').textContent = '■ Stop'; $('#eNote').textContent = 'Recording…';
    const timer = setTimeout(() => rec.state === 'recording' && rec.stop(), EDIT_MAX_SECONDS * 1000);
    rec.ondataavailable = e => e.data.size && chunks.push(e.data);
    rec.onstop = () => { clearTimeout(timer); stream.getTracks().forEach(t => t.stop()); E.rec = null; $('#eRec').textContent = '● Record'; setEditClip(new File(chunks, 'Recording', { type: rec.mimeType || 'audio/webm' })); };
    rec.start();
  } catch { $('#eNote').textContent = 'The microphone is not available. You can upload a file instead.'; }
};

function paintDiff() {
  const ops = diffWords($('#eOrig').value, $('#eTarget').value);
  const box = $('#eDiff');
  box.replaceChildren();
  if (!ops.length) return;
  for (const o of ops) {
    const span = document.createElement('span');
    span.className = o.type;
    span.textContent = o.type === 'swap' ? `${o.a} → ${o.b}` : o.type === 'del' ? o.a : o.type === 'add' ? o.b : o.a;
    box.append(span, ' ');
  }
}
$('#eOrig').oninput = paintDiff; $('#eTarget').oninput = paintDiff;

const b64 = bytes => toBase64(bytes);
async function wavBlob(base64) { return new Blob([Uint8Array.from(atob(base64), c => c.charCodeAt(0))], { type: 'audio/wav' }); }

$('#eGo').onclick = async () => {
  const err = $('#eErr'); err.textContent = '';
  if (!E.bytes) { err.textContent = 'Choose or record a recording first.'; return; }
  if (E.seconds > EDIT_MAX_SECONDS) { err.textContent = `A recording can be at most ${EDIT_MAX_SECONDS} seconds.`; return; }
  if (!$('#eConsent').checked) { err.textContent = 'Please confirm you have the speaker\'s permission.'; return; }
  const target = $('#eTarget').value.trim(), original = $('#eOrig').value.trim();
  if (!original) { err.textContent = 'Type what the recording says in the first box: this browser version cannot recognise speech by itself.'; return; }
  if (!target) { err.textContent = 'Write what the recording should say.'; return; }
  if (!changedCount(diffWords(original, target))) { err.textContent = 'The two texts are the same: nothing to change.'; return; }
  const go = $('#eGo'); go.disabled = true; $('#eCancel').hidden = false;
  E.abort = new AbortController();
  $('#eNote').textContent = 'Starting the voice…';
  try {
    const body = { audio_base64: b64(E.bytes), original_text: original, target_text: target, language: eVoice().code, num_takes: +$('#eTakes').value };
    const r = await call(eVoice(), '/v2/edit', { method: 'POST', headers: JSONH, signal: E.abort.signal, body: JSON.stringify(body) }, t => { $('#eNote').textContent = t; });
    if (!r.ok) throw new Error(await detail(r, `The edit failed (${r.status}).` + ''));
    const doc = await r.json();
    const out = $('#eTakesOut'); out.replaceChildren();
    for (const [i, t] of doc.takes.entries()) {
      const blob = await wavBlob(t.audio_base64), url = URL.createObjectURL(blob);
      const el = document.createElement('div'); el.className = 'take';
      el.innerHTML = '<b></b> <span class="hint"></span><audio controls></audio><a class="small" download=""><button class="small" type="button">Download</button></a>';
      el.querySelector('b').textContent = `Take ${i + 1}`;
      el.querySelector('.hint').textContent = `${t.duration.toFixed(1)} s · ${t.edited_regions.length} region${t.edited_regions.length === 1 ? '' : 's'} re-spoken`;
      el.querySelector('audio').src = url;
      const a = el.querySelector('a'); a.href = url; a.download = `voxa-edit-take-${i + 1}.wav`; a.style.textDecoration = 'none';
      out.append(el);
    }
    $('#eNote').textContent = `Done: ${doc.takes.length} take${doc.takes.length === 1 ? '' : 's'}. Edited regions: ${doc.spans.map(s => s.map(x => x.toFixed(1)).join('–') + ' s').join(', ') || 'none'}.`;
  } catch (e) { err.textContent = e.name === 'AbortError' ? '' : errText(e); $('#eNote').textContent = ''; }
  go.disabled = false; $('#eCancel').hidden = true; E.abort = null;
};
$('#eCancel').onclick = () => E.abort && E.abort.abort();

// ============================================================== cast and scenes
const castList = () => loadList(CAST_KEY).filter(c => c && c.name && VOICES[c.voice]);
function renderCast() {
  const box = $('#cCast'), list = castList();
  box.replaceChildren();
  if (!list.length) { const p = document.createElement('p'); p.className = 'hint'; p.textContent = 'No one in the cast yet. Add a voice below.'; box.append(p); }
  for (const c of list) {
    const m = document.createElement('div'); m.className = 'member';
    const settings = c.state && (Object.values(c.state.sliders || {}).some(Boolean) || c.state.accent || c.state.blend) ? ' · with your settings' : '';
    m.innerHTML = '<b></b><span></span><button class="small" type="button">Remove</button>';
    m.querySelector('b').textContent = c.name;
    m.querySelector('span').textContent = `${VOICES[c.voice].name}${VOICES[c.voice].mine ? '' : ''}${settings}`;
    m.querySelector('button').onclick = () => { saveList(CAST_KEY, loadList(CAST_KEY).filter(x => x.name !== c.name)); renderCast(); };
    box.append(m);
  }
}
$('#cAdd').onclick = () => {
  const name = $('#cName').value.trim();
  if (!name) { $('#cProblems').textContent = 'Give this cast member a name.'; return; }
  const list = loadList(CAST_KEY).filter(c => c.name.toLowerCase() !== name.toLowerCase());
  if (list.length >= 12) { $('#cProblems').textContent = 'A cast can have up to 12 members.'; return; }
  list.push({ name, voice: $('#cVoice').value, state: null });
  saveList(CAST_KEY, list); $('#cName').value = ''; $('#cProblems').textContent = ''; renderCast();
};

async function controlsDoc(voice, status) {
  if (!UI) UI = (await ENGINES.zonosControls(voice, status)).ui;
  return UI;
}
/** The engine fields for a cast member's saved settings. */
async function fieldsFor(member, status) {
  const st = member.state;
  if (!st) return {};
  const ui = await controlsDoc(VOICES[member.voice], status);
  return { ...requestFields(ui, st, ui.request_defaults || {}), ...samplingFields(st.sampling) };
}

const C = { abort: null, url: null };
$('#cGo').onclick = async () => {
  const problems = $('#cProblems'); problems.textContent = '';
  const cast = castList();
  if (!cast.length) { problems.textContent = 'Add at least one voice to the cast first.'; return; }
  const { lines, problems: bad } = parseScript($('#cScript').value, cast.map(c => c.name));
  if (bad.length || !lines.length) { problems.textContent = bad[0] || 'Write the scene first.'; return; }
  const byName = Object.fromEntries(cast.map(c => [c.name, c]));
  const go = $('#cGo'); go.disabled = true; $('#cCancel').hidden = false; $('#cBar').hidden = false;
  C.abort = new AbortController();
  const status = $('#cStatus'), fill = $('#cBar i'), parts = [];
  const gap = new Float32Array(Math.round(24000 * 0.35));
  try {
    const cache = new Map();
    for (const [i, l] of lines.entries()) {
      status.textContent = `Line ${i + 1} of ${lines.length}: ${l.speaker}…`;
      fill.style.width = (100 * i / lines.length) + '%';
      const m = byName[l.speaker];
      if (!cache.has(m.name)) cache.set(m.name, await fieldsFor(m, t => { status.textContent = t; }));
      const fields = cache.get(m.name);
      const pcm = await ENGINES.synth(VOICES[m.voice], l.text, { temperature: 0.7, seed: 4242 + i * 17, signal: C.abort.signal, status: t => { status.textContent = t.startsWith('Loading') || t.startsWith('Downloading') ? t : `Line ${i + 1} of ${lines.length}: ${l.speaker}…`; }, zonos: { fields } });
      if (i) parts.push(gap);
      parts.push(pcm);
    }
    const all = concat(parts);
    const blob = new Blob([ENC.encodeWav(ENC.floatToInt16(all), ENC.SAMPLE_RATE)], { type: 'audio/wav' });
    if (C.url) URL.revokeObjectURL(C.url);
    C.url = URL.createObjectURL(blob);
    $('#cPlay').src = C.url; $('#cPlay').hidden = false;
    const a = $('#cDownload'); a.href = C.url; a.hidden = false;
    fill.style.width = '100%'; status.textContent = `Done: ${lines.length} lines, ${(all.length / 24000).toFixed(0)} seconds.`;
  } catch (e) { status.textContent = ''; if (e.name !== 'AbortError') problems.textContent = errText(e); }
  go.disabled = false; $('#cCancel').hidden = true; setTimeout(() => { $('#cBar').hidden = true; }, 800); C.abort = null;
};
$('#cCancel').onclick = () => C.abort && C.abort.abort();

// -- projects
const PROJECT_LIMIT = 30;
const projects = () => loadList(PROJECTS_KEY);
function currentProject() { return makeProject($('#pName').value.trim() || 'Untitled scene', castList().map(c => ({ name: c.name, voice: c.voice, state: c.state })), $('#cScript').value); }
function openProject(p) {
  const known = p.cast.filter(c => VOICES[c.voice]);
  saveList(CAST_KEY, known.map(c => ({ name: c.name, voice: c.voice, state: c.state })));
  $('#cScript').value = p.script; $('#pName').value = p.name; renderCast();
  const missing = p.cast.length - known.length;
  $('#cProblems').textContent = missing ? `${missing} cast voice${missing === 1 ? ' is' : 's are'} not available on this device (a saved voice that was deleted, perhaps).` : '';
}
function renderProjects() {
  const box = $('#pList'); box.replaceChildren();
  for (const p of projects()) {
    const row = document.createElement('div'); row.className = 'proj';
    row.innerHTML = '<span></span><button class="small" type="button">Open</button><button class="small" type="button">Delete</button>';
    row.querySelector('span').textContent = `${p.name} · ${p.cast.length} voice${p.cast.length === 1 ? '' : 's'}`;
    const [open, del] = row.querySelectorAll('button');
    open.onclick = () => openProject(p);
    del.onclick = () => { saveList(PROJECTS_KEY, projects().filter(x => x !== p && x.name !== p.name)); renderProjects(); };
    box.append(row);
  }
}
$('#pSave').onclick = () => {
  const p = currentProject();
  const list = projects().filter(x => x.name !== p.name);
  if (list.length >= PROJECT_LIMIT) { $('#cProblems').textContent = `You can keep ${PROJECT_LIMIT} projects; delete one first.`; return; }
  list.unshift(p);
  if (!saveList(PROJECTS_KEY, list)) { $('#cProblems').textContent = 'This browser would not let Voxa save the project (storage is full or blocked). Export a file instead.'; return; }
  $('#cProblems').textContent = ''; renderProjects();
};
$('#pExport').onclick = () => {
  const p = currentProject();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([JSON.stringify(p, null, 1)], { type: 'application/json' }));
  a.download = (p.name.replace(/[^\w.-]+/g, '-') || 'scene') + '.voxa-studio.json';
  document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 5000);
};
$('#pImport').onchange = async e => {
  const f = e.target.files[0]; e.target.value = '';
  if (!f) return;
  try {
    if (f.size > 2e6) throw new Error('That file is too large to be a project.');
    openProject(readProject(await f.text()));
  } catch (err) { $('#cProblems').textContent = errText(err); }
};

start().catch(e => { $('#gate').hidden = false; $('#gatemsg').textContent = 'Studio could not start: ' + errText(e); $('#app').hidden = true; });
