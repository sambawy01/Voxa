// Wires the page to the Rust core through Tauri commands.
import {
  charStatus, clampSpeed, speedLabel, friendlyError, tierLabel, formatBytes, groupVoices,
  benchRows, benchFileName, licenceText, languagesOf, previewText, wavSummary,
  toPcm16, recordingLabel, watermarkText,
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
  const what = m.voices ? `${m.voices} voices · ${m.languages.join(', ')}` : 'Audio clean-up';
  meta.textContent = `${what} · ${formatBytes(m.size_bytes)} · ${m.license} · ${m.id} ${m.version}`;
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
  const what = m.voices ? `${m.voices} voices · ${m.languages.join(', ')}` : 'Audio clean-up';
  meta.textContent = `${what} · ${formatBytes(m.size_bytes)} · ${m.license} · ${m.id} ${m.version}`;
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
  await Promise.all([loadVoices(), loadModels(), loadCleanupModels()]);
  await loadCloning();
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

// ---- Clean up audio ----
const cl = {
  open: $('cleanOpen'), file: $('cleanFile'), controls: $('cleanControls'), model: $('cleanModel'),
  run: $('cleanRun'), bar: $('cleanBar'), result: $('cleanResult'), strength: $('cleanStrength'),
  strengthVal: $('cleanStrengthVal'), orig: $('abOriginal'), clean: $('abCleaned'),
  player: $('cleanPlayer'), save: $('cleanSave'), status: $('cleanStatus'), noModel: $('cleanNoModel'),
};
let abCleaned = true;
let previewUrl = null;

async function loadCleanupModels() {
  const models = await invoke('cleanup_models');
  cl.model.replaceChildren(...models.map((m) => new Option(m.family, m.id)));
  cl.noModel.hidden = models.length > 0;
  cl.open.disabled = models.length === 0;
}

async function cleanPreview() {
  const keepTime = cl.player.currentTime || 0;
  const wasPlaying = !cl.player.paused;
  const buf = await invoke('cleanup_preview', {
    cleaned: abCleaned, strength: Number(cl.strength.value), start: 0,
  });
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(new Blob([buf], { type: 'audio/wav' }));
  cl.player.src = previewUrl;
  cl.player.addEventListener('loadedmetadata', () => {
    cl.player.currentTime = Math.min(keepTime, cl.player.duration || keepTime);
    if (wasPlaying) cl.player.play().catch(() => {});
  }, { once: true });
  cl.orig.setAttribute('aria-pressed', String(!abCleaned));
  cl.clean.setAttribute('aria-pressed', String(abCleaned));
}

async function cleanAction(btn, fn) {
  btn.disabled = true;
  cl.status.textContent = '';
  try { await fn(); } catch (e) { cl.status.textContent = friendlyError(e); } finally { btn.disabled = false; }
}

cl.open.addEventListener('click', (ev) => cleanAction(ev.target, async () => {
  const path = await invoke('plugin:dialog|open', { options: {
    title: 'Choose a WAV recording', filters: [{ name: 'WAV audio', extensions: ['wav', 'WAV'] }],
  } });
  if (!path) return;
  const info = await invoke('cleanup_open', { path });
  cl.file.textContent = wavSummary(info);
  cl.controls.hidden = false;
  cl.result.hidden = true;
}));

cl.run.addEventListener('click', (ev) => cleanAction(ev.target, async () => {
  cl.bar.hidden = false;
  cl.bar.value = 0;
  cl.status.textContent = 'Cleaning… this runs on your device.';
  try {
    await invoke('cleanup_run', { model: cl.model.value });
  } finally {
    cl.bar.hidden = true;
  }
  cl.status.textContent = '';
  cl.result.hidden = false;
  abCleaned = true;
  await cleanPreview();
}));

cl.strength.addEventListener('input', () => {
  cl.strengthVal.textContent = `${Math.round(Number(cl.strength.value) * 100)}%`;
});
cl.strength.addEventListener('change', () => { if (abCleaned) cleanPreview().catch((e) => { cl.status.textContent = friendlyError(e); }); });
cl.orig.addEventListener('click', () => { abCleaned = false; cleanPreview().catch((e) => { cl.status.textContent = friendlyError(e); }); });
cl.clean.addEventListener('click', () => { abCleaned = true; cleanPreview().catch((e) => { cl.status.textContent = friendlyError(e); }); });

cl.save.addEventListener('click', (ev) => cleanAction(ev.target, async () => {
  const name = await invoke('cleanup_suggested_name');
  const path = await invoke('plugin:dialog|save', { options: {
    title: 'Save the cleaned recording', defaultPath: name ?? 'cleaned.wav',
    filters: [{ name: 'WAV audio', extensions: ['wav'] }],
  } });
  if (!path) return;
  await invoke('cleanup_save', { path, strength: Number(cl.strength.value) });
  cl.status.textContent = `Saved ${path}: same length, sample rate, channels and timecode as the original, 24-bit.`;
}));

// ---- Your voices (cloning) ----
const cv = {
  box: $('cloneBox'), noModel: $('cloneNoModel'), list: $('cloned'), steps: $('cloneSteps'),
  name: $('cloneName'), start: $('cloneStart'), consentStep: $('cloneConsentStep'),
  sentence: $('cloneSentence'), recConsent: $('cloneRecConsent'), consentTime: $('cloneConsentTime'),
  sampleStep: $('cloneSampleStep'), recSample: $('cloneRecSample'), sampleTime: $('cloneSampleTime'),
  createStep: $('cloneCreateStep'), create: $('cloneCreate'), problems: $('cloneProblems'),
  status: $('cloneStatus'), wm: $('wmCheck'), wmResult: $('wmResult'),
};
const MIN_CONSENT = 4, MIN_SAMPLE = 10, MAX_RECORDING = 60;
const takes = { consent: null, sample: null };
let recorder = null;

/** Records from the microphone until stop() (live only: there is no file upload). */
async function startRecording(onTick) {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 },
  });
  const ctx = new AudioContext();
  const src = ctx.createMediaStreamSource(stream);
  const node = ctx.createScriptProcessor(4096, 1, 1);
  const chunks = [];
  let frames = 0;
  node.onaudioprocess = (e) => {
    const c = new Float32Array(e.inputBuffer.getChannelData(0));
    chunks.push(c);
    frames += c.length;
    onTick(frames / ctx.sampleRate);
  };
  src.connect(node);
  node.connect(ctx.destination);
  return {
    seconds: () => frames / ctx.sampleRate,
    async stop() {
      node.disconnect();
      src.disconnect();
      stream.getTracks().forEach((t) => t.stop());
      const rate = ctx.sampleRate;
      await ctx.close();
      return { samples: toPcm16(chunks), rate, seconds: frames / rate };
    },
  };
}

function setupRecordButton(btn, timeEl, kind, min, idle, after) {
  btn.addEventListener('click', async () => {
    cv.status.textContent = '';
    if (recorder && recorder.kind === kind) {
      const r = await recorder.stop();
      recorder = null;
      btn.textContent = idle;
      btn.classList.remove('rec');
      if (r.seconds < min) {
        timeEl.textContent = `Too short (${r.seconds.toFixed(1)} s). Record again.`;
        return;
      }
      takes[kind] = r;
      timeEl.textContent = `Recorded ${r.seconds.toFixed(1)} s ✓`;
      after();
      return;
    }
    if (recorder) return; // one recording at a time
    try {
      const rec = await startRecording((t) => {
        timeEl.textContent = recordingLabel(t, min);
        if (t >= MAX_RECORDING) btn.click();
      });
      recorder = { ...rec, kind };
      btn.textContent = 'Stop';
      btn.classList.add('rec');
    } catch (e) {
      cv.status.textContent = `The microphone isn't available: ${e?.message ?? e}. Allow Voxa to use the microphone and try again.`;
    }
  });
}

setupRecordButton(cv.recConsent, cv.consentTime, 'consent', MIN_CONSENT, 'Record consent', () => {
  cv.sampleStep.hidden = false;
});
setupRecordButton(cv.recSample, cv.sampleTime, 'sample', MIN_SAMPLE, 'Record voice sample', () => {
  cv.createStep.hidden = false;
});

function resetCloneSteps() {
  takes.consent = takes.sample = null;
  cv.consentStep.hidden = cv.sampleStep.hidden = cv.createStep.hidden = cv.problems.hidden = true;
  cv.consentTime.textContent = cv.sampleTime.textContent = '';
}

cv.start.addEventListener('click', async () => {
  cv.status.textContent = '';
  resetCloneSteps();
  try {
    const c = await invoke('clone_challenge', { name: cv.name.value });
    cv.sentence.textContent = c.sentence;
    cv.consentStep.hidden = false;
  } catch (e) { cv.status.textContent = friendlyError(e); }
});

cv.create.addEventListener('click', async () => {
  cv.create.disabled = true;
  cv.problems.hidden = true;
  cv.status.textContent = 'Checking the consent and making your voice… this runs on your device.';
  try {
    const r = await invoke('clone_create', {
      consent: takes.consent.samples, consentRate: takes.consent.rate,
      sample: takes.sample.samples, sampleRate: takes.sample.rate,
    });
    resetCloneSteps();
    if (r.voice) {
      cv.status.textContent = `"${r.voice.name}" is ready. Pick it under Your voices in the voice list.`;
      cv.name.value = '';
      await loadVoices();
      showCloned();
    } else {
      cv.status.textContent = "The consent check didn't pass. Get a new consent sentence and try again:";
      cv.problems.replaceChildren(...r.problems.map((p) => Object.assign(document.createElement('li'), { textContent: p })));
      cv.problems.hidden = false;
    }
  } catch (e) {
    cv.status.textContent = friendlyError(e);
  } finally {
    cv.create.disabled = false;
  }
});

function showCloned() {
  const mine = voiceList.filter((v) => v.cloned);
  cv.list.replaceChildren(...mine.map((v) => {
    const li = document.createElement('li');
    const info = document.createElement('div');
    const title = document.createElement('div');
    title.className = 'title';
    title.textContent = v.name;
    const flag = document.createElement('span');
    flag.className = 'flag';
    flag.textContent = 'cloned · watermarked';
    title.append(flag);
    info.append(title);
    const remove = document.createElement('button');
    remove.className = 'btn ghost small';
    remove.textContent = 'Delete';
    remove.addEventListener('click', async () => {
      if (!confirm(`Delete the voice "${v.name}"? Its recording and consent record are removed from this device.`)) return;
      try {
        await invoke('remove_cloned', { id: v.id });
        await loadVoices();
        showCloned();
      } catch (e) { cv.status.textContent = friendlyError(e); }
    });
    li.append(info, remove);
    return li;
  }));
}

async function loadCloning() {
  const ready = await invoke('cloning_ready');
  cv.noModel.hidden = ready;
  cv.steps.hidden = !ready;
  showCloned();
}

cv.wm.addEventListener('click', async () => {
  cv.wmResult.textContent = '';
  try {
    const path = await invoke('plugin:dialog|open', { options: {
      title: 'Choose a WAV file to check', filters: [{ name: 'WAV audio', extensions: ['wav', 'WAV'] }],
    } });
    if (!path) return;
    cv.wmResult.textContent = 'Checking…';
    cv.wmResult.textContent = watermarkText(await invoke('watermark_check', { path }));
  } catch (e) { cv.wmResult.textContent = friendlyError(e); }
});

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
  // Phones can't pick a folder, so voice packs come from the catalogue there.
  invoke('platform').then((os) => {
    document.body.dataset.platform = os;
    importBtn.hidden = os === 'android' || os === 'ios';
  });
  loadAll().catch((e) => { status.textContent = friendlyError(e); });
  invoke('licence_status').then(showLicence);
  invoke('catalogue_configured').then((yes) => {
    availableBox.hidden = !yes;
    if (yes) refreshCatalogue();
  });
  window.__TAURI__?.event?.listen('cleanup-progress', ({ payload }) => {
    if (payload.total) cl.bar.value = payload.done / payload.total;
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
