// Wires the page to the Rust core through Tauri commands.
import {
  charStatus, clampSpeed, speedLabel, friendlyError, tierLabel, formatBytes, groupVoices,
} from './logic.js';

const invoke = window.__TAURI__?.core?.invoke;
const $ = (id) => document.getElementById(id);
const text = $('text'), count = $('count'), voice = $('voice'), speed = $('speed');
const speedVal = $('speedVal'), go = $('go'), status = $('status'), player = $('player');
const noVoices = $('noVoices'), models = $('models'), importBtn = $('import'), libStatus = $('libStatus');
let busy = false;
let lastUrl = null;

function refresh() {
  const s = charStatus(text.value);
  count.textContent = s.label;
  count.classList.toggle('bad', s.over);
  go.disabled = busy || !s.ok || !voice.value || !invoke;
}

async function loadVoices() {
  const voices = await invoke('list_voices');
  const keep = voice.value;
  voice.replaceChildren(...groupVoices(voices).map(([label, vs]) => {
    const g = document.createElement('optgroup');
    g.label = label;
    g.append(...vs.map((v) => new Option(v.name, v.id)));
    return g;
  }));
  const fallback = voices.find((v) => v.default) ?? voices[0];
  voice.value = voices.some((v) => v.id === keep) ? keep : (fallback?.id ?? '');
  noVoices.hidden = voices.length > 0;
  voice.disabled = voices.length === 0;
  refresh();
}

function modelItem(m) {
  const li = document.createElement('li');
  const info = document.createElement('div');
  const title = document.createElement('div');
  title.className = 'title';
  title.textContent = `${m.id} ${m.version}`;
  const badge = document.createElement('span');
  badge.className = 'badge';
  badge.textContent = tierLabel(m.tier);
  title.append(badge);
  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.textContent = `${m.voices} voices · ${m.languages.join(', ')} · ${formatBytes(m.size_bytes)} · ${m.license}`;
  const credit = document.createElement('div');
  credit.className = 'meta';
  credit.textContent = m.credit;
  info.append(title, meta, credit);
  const remove = document.createElement('button');
  remove.className = 'btn ghost small';
  remove.textContent = 'Remove';
  remove.addEventListener('click', async () => {
    if (!confirm(`Remove ${m.id} ${m.version} from this device? You can import it again later.`)) return;
    try {
      await invoke('remove_model', { id: m.id, version: m.version });
      await loadAll();
    } catch (e) { libStatus.textContent = friendlyError(e); }
  });
  li.append(info, remove);
  return li;
}

async function loadModels() {
  const list = await invoke('list_models');
  models.replaceChildren(...list.map(modelItem));
}

async function loadAll() {
  await Promise.all([loadVoices(), loadModels()]);
}

async function importPack() {
  let path;
  try {
    path = await invoke('plugin:dialog|open', { options: { directory: true, title: 'Choose a Voxa voice pack folder' } });
  } catch (e) { libStatus.textContent = friendlyError(e); return; }
  if (!path) return;
  importBtn.disabled = true;
  libStatus.textContent = 'Checking and installing the voice pack…';
  try {
    const m = await invoke('import_pack', { path });
    libStatus.textContent = `Installed ${m.id} ${m.version} with ${m.voices} voices.`;
    await loadAll();
  } catch (e) {
    libStatus.textContent = friendlyError(e);
  } finally {
    importBtn.disabled = false;
  }
}

async function generate() {
  busy = true;
  refresh();
  status.textContent = 'Generating…';
  try {
    const buf = await invoke('synthesize', {
      text: text.value,
      voice: voice.value,
      speed: clampSpeed(speed.value),
    });
    if (lastUrl) URL.revokeObjectURL(lastUrl);
    lastUrl = URL.createObjectURL(new Blob([buf], { type: 'audio/wav' }));
    player.src = lastUrl;
    player.hidden = false;
    status.textContent = '';
    await player.play().catch(() => {}); // autoplay may be blocked; the controls remain
  } catch (e) {
    status.textContent = friendlyError(e);
  } finally {
    busy = false;
    refresh();
  }
}

text.addEventListener('input', refresh);
voice.addEventListener('change', refresh);
speed.addEventListener('input', () => { speedVal.textContent = speedLabel(speed.value); });
go.addEventListener('click', generate);
importBtn.addEventListener('click', importPack);
if (invoke) {
  loadAll().catch((e) => { status.textContent = friendlyError(e); });
} else {
  status.textContent = 'Open this page inside the Voxa app.';
}
