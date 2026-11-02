// Wires the page to the Rust core through Tauri commands.
import {
  charStatus, clampSpeed, speedLabel, friendlyError, tierLabel, formatBytes, groupVoices,
} from './logic.js';

const invoke = window.__TAURI__?.core?.invoke;
const $ = (id) => document.getElementById(id);
const text = $('text'), count = $('count'), voice = $('voice'), speed = $('speed');
const speedVal = $('speedVal'), go = $('go'), status = $('status'), player = $('player');
const noVoices = $('noVoices'), models = $('models'), importBtn = $('import'), libStatus = $('libStatus');
const previewBtn = $('preview'), availableBox = $('availableBox'), available = $('available');
const refreshBtn = $('refresh'), creditsBox = $('creditsBox');
let voiceList = [];
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
  voiceList = voices;
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
  previewBtn.disabled = voices.length === 0;
  refresh();
}

function modelItem(m) {
  const li = document.createElement('li');
  const info = document.createElement('div');
  const title = document.createElement('div');
  title.className = 'title';
  title.textContent = m.family;
  const badge = document.createElement('span');
  badge.className = 'badge';
  badge.textContent = tierLabel(m.tier);
  title.append(badge);
  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.textContent = `${m.voices} voices · ${m.languages.join(', ')} · ${formatBytes(m.size_bytes)} · ${m.license} · ${m.id} ${m.version}`;
  if (m.cloning) {
    const flag = document.createElement('span');
    flag.className = 'flag';
    flag.textContent = '✓ voice cloning';
    title.append(flag);
  }
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
  const credits = $('modelCredits');
  credits.replaceChildren(...(list.length ? list : [{ credit: 'No voice models installed yet.', license: '' }]).map((m) => {
    const li = document.createElement('li');
    li.textContent = m.license ? `${m.credit} (${m.license})` : m.credit;
    return li;
  }));
}

function availableItem(m) {
  const li = document.createElement('li');
  const info = document.createElement('div');
  const title = document.createElement('div');
  title.className = 'title';
  title.textContent = m.family;
  const badge = document.createElement('span');
  badge.className = 'badge';
  badge.textContent = tierLabel(m.tier);
  title.append(badge);
  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.textContent = `${m.voices} voices · ${m.languages.join(', ')} · ${formatBytes(m.size_bytes)} · ${m.license} · ${m.id} ${m.version}`;
  const bar = document.createElement('progress');
  bar.max = 1;
  bar.value = 0;
  bar.hidden = true;
  bar.dataset.key = `${m.id}@${m.version}`;
  info.append(title, meta, bar);
  const btn = document.createElement('button');
  btn.className = 'btn small';
  btn.textContent = m.installed ? 'Installed' : 'Install';
  btn.disabled = m.installed;
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    bar.hidden = false;
    btn.textContent = 'Installing…';
    try {
      await invoke('install_model', { id: m.id, version: m.version });
      btn.textContent = 'Installed';
      await loadAll();
    } catch (e) {
      btn.disabled = false;
      btn.textContent = 'Retry';
      libStatus.textContent = friendlyError(e);
    }
  });
  li.append(info, btn);
  return li;
}

async function refreshCatalogue() {
  refreshBtn.disabled = true;
  libStatus.textContent = 'Checking for voices…';
  try {
    const list = await invoke('refresh_catalogue');
    available.replaceChildren(...list.map(availableItem));
    libStatus.textContent = list.length ? '' : 'No downloadable voices for this version yet.';
  } catch (e) {
    libStatus.textContent = friendlyError(e);
  } finally {
    refreshBtn.disabled = false;
  }
}

async function loadThirdParty() {
  if (creditsBox.dataset.loaded) return;
  creditsBox.dataset.loaded = '1';
  try {
    const rows = await (await fetch('third-party.json')).json();
    $('libCount').textContent = `${rows.length} open-source libraries are compiled into Voxa:`;
    $('thirdParty').replaceChildren(...rows.map((r) => {
      const li = document.createElement('li');
      li.textContent = `${r.name} ${r.version}, ${r.license}`;
      return li;
    }));
  } catch {
    $('libCount').textContent = 'The library list could not be loaded.';
  }
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

const previews = new Map();
async function previewVoice() {
  const v = voiceList.find((x) => x.id === voice.value);
  if (!v) return;
  previewBtn.disabled = true;
  try {
    if (!previews.has(v.id)) {
      const buf = await invoke('synthesize', {
        text: `Hi, I'm ${v.name}. This is how I sound.`, voice: v.id, speed: 1,
      });
      previews.set(v.id, URL.createObjectURL(new Blob([buf], { type: 'audio/wav' })));
    }
    player.src = previews.get(v.id);
    player.hidden = false;
    await player.play().catch(() => {});
  } catch (e) {
    status.textContent = friendlyError(e);
  } finally {
    previewBtn.disabled = false;
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
previewBtn.addEventListener('click', previewVoice);
refreshBtn.addEventListener('click', refreshCatalogue);
creditsBox.addEventListener('toggle', () => { if (creditsBox.open) loadThirdParty(); });
if (invoke) {
  loadAll().catch((e) => { status.textContent = friendlyError(e); });
  invoke('catalogue_configured').then((yes) => {
    availableBox.hidden = !yes;
    if (yes) refreshCatalogue();
  });
  window.__TAURI__?.event?.listen('install-progress', ({ payload }) => {
    const bar = document.querySelector(`progress[data-key="${payload.id}@${payload.version}"]`);
    if (bar && payload.total) bar.value = payload.done / payload.total;
  });
} else {
  status.textContent = 'Open this page inside the Voxa app.';
}
