// Wires the page to the Rust core through Tauri commands.
import { charStatus, clampSpeed, speedLabel, friendlyError } from './logic.js';

const invoke = window.__TAURI__?.core?.invoke;
const $ = (id) => document.getElementById(id);
const text = $('text'), count = $('count'), voice = $('voice'), speed = $('speed');
const speedVal = $('speedVal'), go = $('go'), status = $('status'), player = $('player');
let busy = false;
let lastUrl = null;

function refresh() {
  const s = charStatus(text.value);
  count.textContent = s.label;
  count.classList.toggle('bad', s.over);
  go.disabled = busy || !s.ok || !voice.value || !invoke;
}

async function loadVoices() {
  if (!invoke) {
    status.textContent = 'Open this page inside the Voxa app.';
    return;
  }
  const voices = await invoke('list_voices');
  voice.replaceChildren(...voices.map((v) => new Option(v.id, v.id)));
  refresh();
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
loadVoices().catch((e) => { status.textContent = friendlyError(e); });
