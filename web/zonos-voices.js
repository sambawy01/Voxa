// What ZONOS3 users keep on this device (localStorage): saved voices, the cast, and Studio projects.
// Settings only; a cloned clip or a speaker file is never stored.
export const MINE_KEY = 'voxa-zonos-voices';
export const CAST_KEY = 'voxa-zonos-cast';
export const PROJECTS_KEY = 'voxa-zonos-projects';

export function loadList(key) {
  try { const v = JSON.parse(localStorage.getItem(key) || '[]'); return Array.isArray(v) ? v : []; } catch { return []; }
}
export function saveList(key, list) {
  try { localStorage.setItem(key, JSON.stringify(list)); return true; } catch { return false; }
}

/** Add the saved voices (built on a built-in ZONOS3 voice) to `byId`, the page's voice lookup. */
export function registerMine(byId) {
  for (const m of loadList(MINE_KEY)) {
    const base = byId[m.base];
    if (!base || base.engine !== 'zonos') continue;
    byId[m.id] = { ...base, id: m.id, name: m.name + ' ✦', mine: true, state: m.state, description: 'Your saved ZONOS3 voice, based on ' + base.name + '. Desktop only.' };
  }
}
