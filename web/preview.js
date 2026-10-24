// "Hear this voice" buttons, shared by every page with a voice picker. One clip plays at a
// time across the page. Studio voices play the server's preview line (GET /api/preview/<id>,
// made once per voice and free); on-device voices speak the same kind of line in the browser.

// The same lines the server uses for Studio previews (PREVIEW_LINES in server.py).
export const PREVIEW_LINE = (name, lang) => lang === 'z' ? '你好！你的文字用我的声音读出来，就是这样的。'
  : `Hi, I'm ${name}. This is how your words will sound in my voice.`;
// A tiny silent WAV, played inside the tap: iPhones only let a page play sound that a tap
// started, and a clip that arrives after an await would otherwise stay silent.
const SILENCE = 'data:audio/wav;base64,UklGRiYAAABXQVZFZm10IBAAAAABAAEAwF0AAIC7AAACABAAZGF0YQIAAAAAAA==';
const IDLE = '▶ Hear this voice', ON_DEVICE = new Map();
let player = null, current = null;

export function stopPreview() {
  if (player) { player.pause(); player.onended = player.onerror = null; }
  if (current) { current.button.textContent = IDLE; current.note.textContent = ''; }
  current = null;
}

/**
 * Wire a button to preview the voice its picker has selected.
 * voice(): {id, name, lite, key?} for the selected voice, or null.
 * speakOnDevice(voice, note): resolves to a Blob of WAV audio (on-device voices only).
 * key, when given, tells on-device clips apart (e.g. by expressiveness).
 */
export function attachPreview(button, note, voice, speakOnDevice) {
  button.textContent = IDLE;
  const me = { button, note };
  button.onclick = async () => {
    if (current === me) return stopPreview();
    stopPreview();
    const v = voice();
    if (!v || !v.id) return;
    current = me;
    button.textContent = '■ Stop';
    player ||= new Audio();
    player.src = SILENCE;
    player.play().catch(() => {});
    try {
      let src;
      if (v.lite) {
        const key = v.id + ':' + (v.key ?? '');
        if (!ON_DEVICE.has(key)) {
          note.textContent = 'Preparing the on-device voice…';
          const blob = await speakOnDevice(v, note);
          ON_DEVICE.set(key, URL.createObjectURL(blob));
        }
        src = ON_DEVICE.get(key);
      } else {
        note.textContent = 'Loading…';
        src = '/api/preview/' + encodeURIComponent(v.id);
      }
      if (current !== me) return;
      player.src = src;
      player.onended = () => { if (current === me) stopPreview(); };
      player.onerror = () => { if (current === me) { stopPreview(); note.textContent = 'The preview could not play. Try again in a moment.'; } };
      await player.play();
      if (current === me) note.textContent = `${v.name} · ${v.lite ? 'on this device' : 'Studio voice'}`;
    } catch (e) {
      if (current !== me) return;
      stopPreview();
      note.textContent = 'The preview could not play: ' + e.message;
    }
  };
  return me;
}
