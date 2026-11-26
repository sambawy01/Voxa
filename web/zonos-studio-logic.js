// Pure helpers for the ZONOS3 Studio page (speech edit, cast and projects, speaker files, streaming).
// No browser APIs here, so they are tested in Node (tests/zonos-studio.test.mjs).

// -- speech edit: what will change -----------------------------------------------------------

const words = s => (s || '').trim().split(/\s+/).filter(Boolean);
const norm = w => w.toLowerCase().replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, '');

/**
 * Word diff of the original against the target (longest common subsequence on words, ignoring case
 * and edge punctuation). Returns [{type: 'keep'|'del'|'add'|'swap', a?, b?}]; a del next to an add is a swap.
 */
export function diffWords(original, target) {
  const A = words(original), B = words(target), n = A.length, m = B.length;
  const L = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) {
    L[i][j] = norm(A[i]) === norm(B[j]) ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  }
  const ops = [];
  let i = 0, j = 0;
  while (i < n || j < m) {
    if (i < n && j < m && norm(A[i]) === norm(B[j])) { ops.push({ type: 'keep', a: A[i], b: B[j] }); i++; j++; }
    else if (j < m && (i === n || L[i][j + 1] >= L[i + 1][j])) ops.push({ type: 'add', b: B[j++] });
    else ops.push({ type: 'del', a: A[i++] });
  }
  const out = [];
  for (let k = 0; k < ops.length; k++) {
    const o = ops[k], nx = ops[k + 1];
    if (o.type === 'del' && nx && nx.type === 'add') { out.push({ type: 'swap', a: o.a, b: nx.b }); k++; }
    else if (o.type === 'add' && nx && nx.type === 'del') { out.push({ type: 'swap', a: nx.a, b: o.b }); k++; }
    else out.push(o);
  }
  return out;
}
export const changedCount = ops => ops.filter(o => o.type !== 'keep').length;

// -- cast scripts ----------------------------------------------------------------------------

export const MAX_LINES = 400;
export const MAX_LINE_CHARS = 1500;

/**
 * "Name: text" lines to [{speaker, text}]. A line without a known name continues the previous
 * speaker's turn. Returns {lines, problems}; problems are human-readable.
 */
export function parseScript(text, castNames) {
  const known = new Map(castNames.map(n => [n.toLowerCase(), n]));
  const lines = [], problems = [];
  let n = 0;
  for (const raw of String(text || '').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    n++;
    const m = /^([^:：]{1,40})[:：]\s*(.*)$/.exec(line);
    if (m && known.has(m[1].trim().toLowerCase())) {
      if (m[2]) lines.push({ speaker: known.get(m[1].trim().toLowerCase()), text: m[2] });
      else problems.push(`Line ${n}: ${m[1].trim()} has nothing to say.`);
    } else if (m && /^[\p{L}\p{N} ._-]{1,24}$/u.test(m[1]) && !/\s{2,}/.test(m[1]) && lines.length === 0) {
      problems.push(`Line ${n}: "${m[1].trim()}" is not in the cast. Add them first, or fix the name.`);
    } else if (lines.length) {
      lines[lines.length - 1].text += ' ' + line;
    } else {
      problems.push(`Line ${n}: start each line with a cast name and a colon, like "${castNames[0] || 'Name'}: Hello."`);
    }
  }
  if (lines.length > MAX_LINES) problems.push(`That is ${lines.length} lines; the limit is ${MAX_LINES}.`);
  for (const l of lines) if (l.text.length > MAX_LINE_CHARS) { problems.push(`A line by ${l.speaker} is longer than ${MAX_LINE_CHARS} characters; split it.`); break; }
  return { lines, problems };
}

// -- projects (saved on this device, or exported as a file) -------------------------------------------

export const PROJECT_VERSION = 1;
const str = (v, max) => (typeof v === 'string' ? v.slice(0, max) : '');

/** A project document: the cast and the script. Cast members refer to voices by id; state is the saved Advanced settings. */
export function makeProject(name, cast, script) {
  return { v: PROJECT_VERSION, name: str(name, 80) || 'Untitled', cast: cast.map(c => ({ name: str(c.name, 40), voice: str(c.voice, 80), state: c.state || null })), script: str(script, 200000) };
}

/** A project file from disk, checked and trimmed; throws a readable Error when it is not one of ours. */
export function readProject(json) {
  let doc;
  try { doc = typeof json === 'string' ? JSON.parse(json) : json; } catch { throw new Error('That file is not a Voxa Studio project.'); }
  if (!doc || doc.v !== PROJECT_VERSION || !Array.isArray(doc.cast) || typeof doc.script !== 'string') throw new Error('That file is not a Voxa Studio project.');
  const cast = doc.cast.slice(0, 12).map(c => ({ name: str(c && c.name, 40), voice: str(c && c.voice, 80), state: c && typeof c.state === 'object' ? c.state : null })).filter(c => c.name && c.voice);
  return makeProject(doc.name, cast, doc.script);
}

// -- speaker files (.npy) ----------------------------------------------------------------------

/** A float32 vector as a NumPy .npy file (version 1.0), which the engine's speaker upload reads. */
export function toNpy(values) {
  const f = values instanceof Float32Array ? values : Float32Array.from(values);
  const head = `{'descr': '<f4', 'fortran_order': False, 'shape': (${f.length},), }`;
  const total = Math.ceil((10 + head.length + 1) / 64) * 64;
  const header = head.padEnd(total - 10 - 1, ' ') + '\n';
  const out = new Uint8Array(10 + header.length + f.length * 4);
  out.set([0x93, 0x4e, 0x55, 0x4d, 0x50, 0x59, 1, 0, header.length & 255, header.length >> 8]);
  for (let i = 0; i < header.length; i++) out[10 + i] = header.charCodeAt(i);
  new Uint8Array(out.buffer, 10 + header.length).set(new Uint8Array(f.buffer, f.byteOffset, f.byteLength));
  return out;
}

/** The float32 values of a .npy file: one-dimensional (or 1 x N), little-endian float32 or float64. */
export function fromNpy(bytes) {
  const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  if (b.length < 12 || b[0] !== 0x93 || String.fromCharCode(...b.subarray(1, 6)) !== 'NUMPY') throw new Error('That is not a .npy speaker file.');
  const v = b[6];
  const hlen = v === 1 ? b[8] | (b[9] << 8) : (b[8] | (b[9] << 8) | (b[10] << 16) | (b[11] << 24));
  const start = (v === 1 ? 10 : 12);
  const header = String.fromCharCode(...b.subarray(start, start + hlen));
  const descr = /'descr':\s*'([^']+)'/.exec(header)?.[1];
  const shape = /'shape':\s*\(([^)]*)\)/.exec(header)?.[1].split(',').map(s => s.trim()).filter(Boolean).map(Number) || [];
  if (/'fortran_order':\s*True/.test(header)) throw new Error('That speaker file is stored column by column, which is not supported.');
  if (!['<f4', '<f8'].includes(descr)) throw new Error('A speaker file must hold float numbers (float32 or float64).');
  const n = shape.reduce((a, x) => a * x, 1);
  if (!(shape.length === 1 || (shape.length === 2 && shape[0] === 1)) || !Number.isInteger(n) || n < 8 || n > 65536) throw new Error('That speaker file does not hold a single speaker vector.');
  const data = b.slice(start + hlen);
  const size = descr === '<f4' ? 4 : 8;
  if (data.length < n * size) throw new Error('That speaker file is cut short.');
  const dv = new DataView(data.buffer);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) out[i] = descr === '<f4' ? dv.getFloat32(i * 4, true) : dv.getFloat64(i * 8, true);
  if (out.some(x => !Number.isFinite(x))) throw new Error('That speaker file contains invalid numbers.');
  return out;
}

// -- streaming ---------------------------------------------------------------------------------

/** Turns arbitrary byte chunks of little-endian float32 PCM into whole samples. */
export class FloatStream {
  constructor() { this.rest = new Uint8Array(0); }
  push(chunk) {
    const b = new Uint8Array(this.rest.length + chunk.length);
    b.set(this.rest); b.set(chunk, this.rest.length);
    const whole = b.length - (b.length % 4);
    this.rest = b.slice(whole);
    const out = new Float32Array(whole / 4);
    const dv = new DataView(b.buffer, 0, whole);
    for (let i = 0; i < out.length; i++) out[i] = dv.getFloat32(i * 4, true);
    return out;
  }
}

/** Join Float32Arrays. */
export function concat(parts) {
  const out = new Float32Array(parts.reduce((a, p) => a + p.length, 0));
  let at = 0;
  for (const p of parts) { out.set(p, at); at += p.length; }
  return out;
}
