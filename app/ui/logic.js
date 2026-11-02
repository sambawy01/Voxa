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
  return msg ? `Something went wrong: ${msg}` : 'Something went wrong.';
}
