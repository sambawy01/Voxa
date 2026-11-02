// Wires the page to the Rust core through Tauri commands.
import {
  charStatus, clampSpeed, speedLabel, friendlyError, tierLabel, formatBytes, groupVoices,
  benchRows, benchFileName, licenceText, languagesOf, previewText,
} from './logic.js';

const invoke = window.__TAURI__?.core?.invoke;
const $ = (id) => document.getElementById(id);
const text = $('text'), count = $('count'), voice = $('voice'), speed = $('speed');
const speedVal = $('speedVal'), go = $('go'), status = $('status'), player = $('player');
const noVoices = $('noVoices'), models = $('models'), importBtn = $('import'), libStatus = $('libStatus');
const previewBtn = $('preview'), availableBox = $('availableBox'), available = $('available');
const refreshBtn = $('refresh'), creditsBox = $('creditsBox');
let voiceList = [];
const language = $('language');
let busy = false;
let lastUrl = null;

function refresh() {
  const s = charStatus(text.value);
  count.textContent = s.label;
  count.classList.toggle('bad', s.over);
  go.disabled = busy || !s.ok || !voice.value || !invoke;
}

/** Fills the voice picker with the chosen language's voices. */
function showVoices(keep) {
  const voices = voiceList.filter((v) => v.language === language.value);
  voice.replaceChildren(...groupVoices(voices).map(([label, vs]) => {
    const g = document.createElement('optgroup');
    g.label = label;
    g.append(...vs.map((v) => new Option(v.name, v.id)));
    return g;
  }));
  const fallback = voices.find((v) => v.default) ?? voices[0];
  voice.value = voices.some((v) => v.id === keep) ? keep : (fallback?.id ?? '');
}

async function loadVoices() {
  const voices = await invoke('list_voices');
  voiceList = voices;
  const keep = voice.value;
  const keepLang = language.value || voices.find((v) => v.default)?.language;
  const langs = languagesOf(voices);
  language.replaceChildren(...langs.map(([tag, name]) => new Option(name, tag)));
  language.value = langs.some(([t]) => t === keepLang) ? keepLang : (langs[0]?.[0] ?? '');
  language.disabled = langs.length < 2;
  showVoices(keep);
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
    if (m.restrictions) {
      // Licences such as Open RAIL-M require these use restrictions to be passed on.
      const d = document.createElement('details');
      const sum = document.createElement('summary');
      sum.className = 'dim note';
      sum.textContent = 'Use restrictions that come with this voice';
      const pre = document.createElement('pre');
      pre.className = 'restrictions';
      pre.textContent = m.restrictions;
      d.append(sum, pre);
      li.append(d);
    }
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

const benchBtn = $('bench'), benchBar = $('benchBar'), benchOut = $('benchOut');

function benchTable(report) {
  const table = document.createElement('table');
  table.className = 'results';
  const head = table.createTHead().insertRow();
  for (const h of ['Voice model', 'Load', 'First audio', 'Speed', 'Memory']) {
    const th = document.createElement('th');
    th.textContent = h;
    head.append(th);
  }
  const body = table.createTBody();
  for (const r of benchRows(report)) {
    const row = body.insertRow();
    for (const v of [r.model, r.load, r.first, r.speed, r.memory]) row.insertCell().textContent = v;
  }
  const wrap = document.createElement('div');
  wrap.className = 'scroll-x';
  wrap.append(table);
  const d = report.device;
  const device = document.createElement('p');
  device.className = 'dim note';
  device.textContent = `${d.os} · ${d.cpu || d.arch} · ${d.cpus} cores · ${formatBytes(d.ram_mb * 1e6)} RAM`;
  const save = document.createElement('a');
  save.className = 'btn ghost small';
  save.textContent = 'Save results';
  save.download = benchFileName(report);
  save.href = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }));
  return [device, wrap, save];
}

async function runBench() {
  benchBtn.disabled = true;
  benchBar.hidden = false;
  benchBar.value = 0;
  benchOut.replaceChildren(document.createTextNode('Running… this loads each model and reads a short paragraph.'));
  try {
    const report = await invoke('run_benchmark');
    benchOut.replaceChildren(...(report.results.length ? benchTable(report)
      : [document.createTextNode('Install a voice first, then run the speed test.')]));
  } catch (e) {
    benchOut.textContent = friendlyError(e);
  } finally {
    benchBtn.disabled = false;
    benchBar.hidden = true;
  }
}

const licenceBox = $('licenceBox'), licenceState = $('licenceState'), licenceMsg = $('licenceMsg');

function showLicence(s) {
  licenceBox.hidden = !s.enabled;
  licenceState.textContent = licenceText(s);
  $('licenceActivate').hidden = s.active;
  $('licenceActive').hidden = !s.active;
}

async function licenceAction(btn, fn) {
  btn.disabled = true;
  licenceMsg.textContent = '';
  try { await fn(); } catch (e) { licenceMsg.textContent = friendlyError(e); } finally { btn.disabled = false; }
}

$('activate').addEventListener('click', (ev) => licenceAction(ev.target, async () => {
  showLicence(await invoke('activate_licence', { key: $('licenceKey').value }));
  $('licenceKey').value = '';
}));
$('deactivate').addEventListener('click', (ev) => licenceAction(ev.target, async () => {
  if (!confirm('Deactivate Voxa on this device? You can activate it again, here or on another device.')) return;
  showLicence(await invoke('deactivate_licence'));
}));
$('reset').addEventListener('click', (ev) => licenceAction(ev.target, async () => {
  if (!confirm('Release every device on this licence? Each one will need to be activated again.')) return;
  const left = await invoke('reset_licence', { key: $('resetKey').value });
  licenceMsg.textContent = `All devices released. Resets left this year: ${left}.`;
  showLicence(await invoke('licence_status'));
}));

const previews = new Map();
async function previewVoice() {
  const v = voiceList.find((x) => x.id === voice.value);
  if (!v) return;
  previewBtn.disabled = true;
  try {
    if (!previews.has(v.id)) {
      const buf = await invoke('synthesize', {
        text: previewText(v.name, v.language), voice: v.id, speed: 1,
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
language.addEventListener('change', () => { showVoices(); refresh(); });
speed.addEventListener('input', () => { speedVal.textContent = speedLabel(speed.value); });
go.addEventListener('click', generate);
importBtn.addEventListener('click', importPack);
previewBtn.addEventListener('click', previewVoice);
refreshBtn.addEventListener('click', refreshCatalogue);
benchBtn.addEventListener('click', runBench);
creditsBox.addEventListener('toggle', () => { if (creditsBox.open) loadThirdParty(); });
if (invoke) {
  loadAll().catch((e) => { status.textContent = friendlyError(e); });
  invoke('licence_status').then(showLicence);
  invoke('catalogue_configured').then((yes) => {
    availableBox.hidden = !yes;
    if (yes) refreshCatalogue();
  });
  window.__TAURI__?.event?.listen('bench-progress', ({ payload }) => {
    if (payload.total) benchBar.value = payload.done / payload.total;
  });
  window.__TAURI__?.event?.listen('install-progress', ({ payload }) => {
    const bar = document.querySelector(`progress[data-key="${payload.id}@${payload.version}"]`);
    if (bar && payload.total) bar.value = payload.done / payload.total;
  });
} else {
  status.textContent = 'Open this page inside the Voxa app.';
}
