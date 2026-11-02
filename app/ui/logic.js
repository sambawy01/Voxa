// Pure UI logic, kept free of the DOM and Tauri so it runs under `node --test`.

export const MAX_CHARS = 4000; // matches voxa-core::text::MAX_CHARS

/** Character counter text and whether the text can be generated. */
export function charStatus(text) {
  const n = [...text.trim()].length; // count characters, not UTF-16 units
  return {
    count: n,
    label: `${n.toLocaleString('en-US')} / ${MAX_CHARS.toLocaleString('en-US')}`,
    ok: n > 0 && n <= MAX_CHARS,
    over: n > MAX_CHARS,
  };
}

/** Clamps a speed slider value to the range the engine accepts. */
export function clampSpeed(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return 1;
  return Math.min(2, Math.max(0.5, Math.round(n * 20) / 20));
}

/** "1.25×" style label. */
export function speedLabel(v) {
  return `${clampSpeed(v).toFixed(2).replace(/0$/, '')}×`;
}

/** Turns an engine error string into a sentence for the user. */
export function friendlyError(err) {
  const msg = String(err?.message ?? err ?? '');
  if (/text is empty/.test(msg)) return 'Type something to read aloud first.';
  if (/limit is/.test(msg)) return `That's too long for one go: the limit is ${MAX_CHARS.toLocaleString('en-US')} characters.`;
  if (/unknown voice/.test(msg)) return 'That voice is not installed on this device.';
  if (/not activated/.test(msg)) return 'Activate Voxa under Licence to start reading.';
  if (/could not reach the licence server/.test(msg)) return 'Could not reach the licence server. Check your connection and try again; once activated, Voxa works offline.';
  return msg ? `Something went wrong: ${msg}` : 'Something went wrong.';
}

/** Device label from the plan: 📱 Phone, 💻 Computer, 🖥️ GPU. */
export function tierLabel(tier) {
  return { phone: '📱 Phone', computer: '💻 Computer', gpu: '🖥️ GPU' }[tier] ?? tier;
}

/** "148.5 MB" style size. */
export function formatBytes(n) {
  if (!Number.isFinite(n) || n < 0) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
  return `${i === 0 ? n : n.toFixed(1)} ${units[i]}`;
}

/** Groups voices for the picker: [[label, voices], ...]. Groups are per family and
 * accent ("Voxa Classic · American women"); within a family women come first; names sorted. */
export function groupVoices(voices) {
  const groups = new Map();
  for (const v of voices) {
    const who = `${v.accent} ${v.gender === 'female' ? 'women' : v.gender === 'male' ? 'men' : 'voices'}`;
    const label = v.family ? `${v.family} · ${who}` : who;
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(v);
  }
  return [...groups.entries()]
    .map(([label, vs]) => [label, vs.sort((a, b) => a.name.localeCompare(b.name))])
    .sort(([a], [b]) => {
      const family = (l) => (l.includes(' · ') ? l.split(' · ')[0] : '');
      const fa = family(a), fb = family(b);
      if (fa !== fb) return fa.localeCompare(fb);
      if (a.endsWith('women') !== b.endsWith('women')) return a.endsWith('women') ? -1 : 1;
      return a.localeCompare(b);
    });
}

/** Rows for the speed-test table from a benchmark report. */
export function benchRows(report) {
  return report.results.map((r) => ({
    model: `${r.family} (${r.voice})`,
    load: `${(r.load_ms / 1000).toFixed(1)} s`,
    first: `${(r.first_audio_ms / 1000).toFixed(1)} s`,
    speed: r.rtf > 0 ? `${(1 / r.rtf).toFixed(1)}× real time` : '–',
    memory: `${r.model_rss_mb} MB`,
  }));
}

/** File name for saving a report, e.g. "voxa-speed-test-x86_64-2026-11-02.json". */
export function benchFileName(report, date = new Date()) {
  return `voxa-speed-test-${report.device.arch}-${date.toISOString().slice(0, 10)}.json`;
}

/** One sentence describing the licence state. */
export function licenceText(s) {
  if (!s.enabled) return '';
  if (s.active) {
    const when = s.issued ? ` on ${s.issued.slice(0, 10)}` : '';
    return `Activated on this device${when}. Voxa now works offline. Your licence covers ${s.seats ?? 2} devices.`;
  }
  if (s.problem) return `The saved licence can't be used: ${s.problem}. Activate again with your key.`;
  return 'Enter the licence key from your purchase email to activate Voxa on this device.';
}
