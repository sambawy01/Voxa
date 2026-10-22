// Voxa audiobooks: turn a PDF or text file into reviewed chapters, then into speech chunks.
// The text functions are pure so they can be tested without a browser (tests/book.test.mjs).

// Characters of text per second of speech, measured on Kokoro: 4,000 characters gave 268 s.
export const CHARS_PER_SECOND = 15;
export const CHUNK_CHARS = 1500;

const SENTENCE_END = /[.!?…:]["”’)\]]?$/;
const HEADING = /^(chapter|part|book|prologue|epilogue|introduction|preface|foreword|afterword|appendix)\b[^\n]{0,70}$/i;
const PAGE_NUMBER = /^\s*(page\s*)?[0-9ivxlc]{1,6}(\s*(of|\/)\s*[0-9]{1,6})?\s*$/i;

/** Group pdf.js text items into lines, top to bottom: [{text, y, h}]. */
export function linesFromItems(items) {
  const rows = [];
  for (const it of items) {
    if (!it.str && !it.hasEOL) continue;
    const y = it.transform[5], x = it.transform[4], h = Math.abs(it.transform[3]) || it.height || 10;
    let row = rows.find(r => Math.abs(r.y - y) <= Math.max(2, h * 0.3));
    if (!row) rows.push(row = { y, h, parts: [] });
    row.parts.push({ x, w: it.width || 0, str: it.str });
    row.h = Math.max(row.h, h);
  }
  rows.sort((a, b) => b.y - a.y);
  return rows.map(r => {
    r.parts.sort((a, b) => a.x - b.x);
    let text = '', end = null;
    for (const p of r.parts) {
      // pdf.js often splits words into runs; add a space only where the runs leave a gap.
      if (end !== null && p.x - end > r.h * 0.15 && !text.endsWith(' ') && !p.str.startsWith(' ')) text += ' ';
      text += p.str;
      end = p.x + p.w;
    }
    return { text: text.replace(/\s+/g, ' ').trim(), y: r.y, h: r.h };
  }).filter(l => l.text);
}

const normalise = s => s.toLowerCase().replace(/[0-9]+/g, '#').replace(/\s+/g, ' ').trim();

/**
 * Remove running headers, footers and page numbers. `pages` is [[{text, y, h}]]. A line in
 * the top or bottom two of a page is a header or footer when the same text (ignoring
 * digits) sits there on at least 40% of pages, or when it is only a page number.
 */
export function cleanPages(pages) {
  const EDGE = 2;
  const counts = new Map();
  for (const lines of pages) {
    const seen = new Set();
    for (const l of [...lines.slice(0, EDGE), ...lines.slice(-EDGE)]) seen.add(normalise(l.text));
    for (const k of seen) counts.set(k, (counts.get(k) || 0) + 1);
  }
  const need = Math.max(3, Math.ceil(pages.length * 0.4));
  return pages.map(lines => lines.filter((l, i) => {
    const atEdge = i < EDGE || i >= lines.length - EDGE;
    if (!atEdge) return true;
    if (PAGE_NUMBER.test(l.text)) return false;
    return pages.length < 3 || (counts.get(normalise(l.text)) || 0) < need;
  }));
}

/** Join a page's lines into paragraphs, mending words hyphenated across lines. */
export function paragraphsFromLines(lines) {
  if (!lines.length) return [];
  const gaps = [];
  for (let i = 1; i < lines.length; i++) gaps.push(lines[i - 1].y - lines[i].y);
  const typical = gaps.length ? [...gaps].sort((a, b) => a - b)[Math.floor(gaps.length / 2)] : 0;
  const longest = Math.max(...lines.map(l => l.text.length));
  const paras = [];
  let cur = '';
  lines.forEach((l, i) => {
    const prev = lines[i - 1];
    const gap = prev ? prev.y - l.y : 0;
    const breakHere = prev && (gap > typical * 1.6 ||
      (SENTENCE_END.test(prev.text) && prev.text.length < longest * 0.75) ||
      HEADING.test(l.text) || HEADING.test(prev.text));
    if (breakHere) { paras.push(cur); cur = ''; }
    cur = joinLine(cur, l.text);
  });
  if (cur) paras.push(cur);
  return paras.map(p => p.trim()).filter(Boolean);
}

/** Append a line to running text: "exam-" + "ple" -> "example"; otherwise a space. */
export function joinLine(text, line) {
  line = line.replace(/­/g, '');
  if (!text) return line;
  if (/[A-Za-zÀ-ÿ]-$/.test(text) && /^[a-zà-ÿ]/.test(line)) return text.slice(0, -1) + line;
  return text + ' ' + line;
}

/** Paragraphs of plain text: blank lines separate paragraphs; single newlines are joined. */
export function paragraphsFromText(text) {
  return text.replace(/\r\n?/g, '\n').split(/\n\s*\n/)
    .map(block => block.split('\n').reduce((acc, l) => joinLine(acc, l.trim()), '').trim())
    .filter(Boolean);
}

/**
 * Chapters from page-anchored paragraphs. `paras` is [{text, page}]; `starts`, if given, is
 * the PDF outline: [{title, page}]. Without an outline, headings such as "Chapter 3" start
 * chapters. Anything before the first chapter is front matter, excluded by default.
 */
export function findChapters(paras, starts = []) {
  const chapters = [];
  const open = (title, include, page) => { chapters.push({ title, include, page, paragraphs: [] }); };
  const sorted = [...starts].filter(s => Number.isInteger(s.page)).sort((a, b) => a.page - b.page);
  if (sorted.length) {
    let next = 0;
    for (const p of paras) {
      while (next < sorted.length && p.page >= sorted[next].page) {
        open(sorted[next].title.trim() || `Chapter ${chapters.length + 1}`, true, sorted[next].page);
        next++;
      }
      if (!chapters.length) open('Opening pages', false, p.page);
      chapters[chapters.length - 1].paragraphs.push(p.text);
    }
  } else {
    for (const p of paras) {
      // The heading stays in the text too, so each chapter is announced when read aloud.
      if (HEADING.test(p.text)) open(p.text, true, p.page);
      if (!chapters.length) open('Opening pages', false, p.page);
      chapters[chapters.length - 1].paragraphs.push(p.text);
    }
    // No headings at all: the whole text is the book.
    if (chapters.length === 1 && !chapters[0].include) { chapters[0].title = 'Full text'; chapters[0].include = true; }
  }
  return chapters.filter(c => c.paragraphs.length).map(c => ({ ...c, text: c.paragraphs.join('\n\n') }));
}

/** Split text into chunks of at most `max` characters at paragraph, then sentence, then word boundaries. */
export function splitChunks(text, max = CHUNK_CHARS) {
  const pieces = [];
  for (const para of text.split(/\n\s*\n/).map(s => s.trim()).filter(Boolean)) {
    if (para.length <= max) { pieces.push(para); continue; }
    for (const sentence of para.split(/(?<=[.!?…]["”’)\]]?)\s+/)) {
      if (sentence.length <= max) { pieces.push(sentence); continue; }
      let rest = sentence;
      while (rest.length > max) {
        const cut = rest.lastIndexOf(' ', max) > max * 0.5 ? rest.lastIndexOf(' ', max) : max;
        pieces.push(rest.slice(0, cut).trim());
        rest = rest.slice(cut).trim();
      }
      if (rest) pieces.push(rest);
    }
  }
  // Pack small pieces together, keeping paragraph breaks as the joins.
  const chunks = [];
  for (const piece of pieces) {
    const last = chunks[chunks.length - 1];
    if (last !== undefined && last.length + 2 + piece.length <= max) chunks[chunks.length - 1] = last + '\n\n' + piece;
    else chunks.push(piece);
  }
  return chunks;
}

export const estimateSeconds = chars => chars / CHARS_PER_SECOND;

export function formatDuration(seconds) {
  const m = Math.round(seconds / 60);
  if (m < 60) return `${Math.max(1, m)} min`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')} min`;
}

/** Read a PDF with pdf.js: page-anchored paragraphs plus the outline's chapter starts. */
export async function readPdf(pdfjs, data, onProgress = () => {}, options = {}) {
  const doc = await pdfjs.getDocument({ ...options, data, isEvalSupported: false }).promise;
  const pages = [];
  for (let n = 1; n <= doc.numPages; n++) {
    const page = await doc.getPage(n);
    pages.push(linesFromItems((await page.getTextContent()).items));
    onProgress(n, doc.numPages);
  }
  const cleaned = cleanPages(pages);
  const paras = [];
  cleaned.forEach((lines, i) => {
    const ps = paragraphsFromLines(lines);
    // A paragraph that runs over a page break continues on the next page.
    ps.forEach((text, j) => {
      const prev = paras[paras.length - 1];
      if (j === 0 && prev && !SENTENCE_END.test(prev.text) && /^[a-zà-ÿ]/.test(text)) prev.text = joinLine(prev.text, text);
      else paras.push({ text, page: i + 1 });
    });
  });
  const starts = [];
  for (const item of (await doc.getOutline()) || []) {
    try {
      const dest = typeof item.dest === 'string' ? await doc.getDestination(item.dest) : item.dest;
      if (dest && dest[0]) starts.push({ title: item.title, page: (await doc.getPageIndex(dest[0])) + 1 });
    } catch (e) { /* an outline entry pointing nowhere is skipped */ }
  }
  return { pages: doc.numPages, chapters: findChapters(paras, starts), outline: starts.length > 0 };
}

// --- Audio ---------------------------------------------------------------------------------
// Both engines produce 24 kHz mono 16-bit speech; chapters are joined as raw samples.

export const SAMPLE_RATE = 24000;
export const PAUSE_BETWEEN_CHUNKS = 0.4;  // seconds; chunks end at paragraph boundaries

/** Samples from a 16-bit PCM WAV file (any chunk layout). */
export function parseWav(buffer) {
  const v = new DataView(buffer instanceof ArrayBuffer ? buffer : buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength));
  const tag = o => String.fromCharCode(v.getUint8(o), v.getUint8(o + 1), v.getUint8(o + 2), v.getUint8(o + 3));
  if (v.byteLength < 12 || tag(0) !== 'RIFF' || tag(8) !== 'WAVE') throw new Error('not a WAV file');
  let fmt = null;
  for (let o = 12; o + 8 <= v.byteLength;) {
    const id = tag(o), size = v.getUint32(o + 4, true), body = o + 8;
    if (id === 'fmt ') {
      fmt = { format: v.getUint16(body, true), channels: v.getUint16(body + 2, true),
              rate: v.getUint32(body + 4, true), bits: v.getUint16(body + 14, true) };
    } else if (id === 'data') {
      if (!fmt || fmt.format !== 1 || fmt.bits !== 16 || fmt.channels !== 1) throw new Error('expected mono 16-bit PCM');
      const end = Math.min(body + size, v.byteLength);
      return { rate: fmt.rate, pcm: new Int16Array(v.buffer.slice(body, body + ((end - body) & ~1))) };
    }
    o = body + size + (size & 1);
  }
  throw new Error('WAV file has no audio data');
}

/** A 16-bit mono WAV file (bytes) from samples. */
export function encodeWav(pcm, rate = SAMPLE_RATE) {
  const out = new Uint8Array(44 + pcm.length * 2), v = new DataView(out.buffer);
  const put = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  put(0, 'RIFF'); v.setUint32(4, 36 + pcm.length * 2, true); put(8, 'WAVE');
  put(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  put(36, 'data'); v.setUint32(40, pcm.length * 2, true);
  new Int16Array(out.buffer, 44).set(pcm);
  return out;
}

export function floatToInt16(f32) {
  const out = new Int16Array(f32.length);
  for (let i = 0; i < f32.length; i++) out[i] = Math.max(-32768, Math.min(32767, Math.round(f32[i] * 32767)));
  return out;
}

/** Join chunks with `pause` seconds of silence between them. */
export function joinPcm(parts, rate = SAMPLE_RATE, pause = PAUSE_BETWEEN_CHUNKS) {
  const gap = Math.round(rate * pause);
  const out = new Int16Array(parts.reduce((a, p) => a + p.length, 0) + gap * Math.max(0, parts.length - 1));
  let at = 0;
  parts.forEach((p, i) => { if (i) at += gap; out.set(p, at); at += p.length; });
  return out;
}

/** Every chunk to generate, in reading order: [{chapter, index, text}]. */
export function planChunks(chapters, max = CHUNK_CHARS) {
  const plan = [];
  chapters.forEach((c, chapter) => {
    if (c.include) splitChunks(c.text, max).forEach((text, index) => plan.push({ chapter, index, text }));
  });
  return plan;
}

/** A fingerprint of what will be read and how, so saved progress is only reused for the same book. */
export async function bookKey(chapters, settings) {
  const data = JSON.stringify({ settings, chapters: chapters.filter(c => c.include).map(c => [c.title, c.text]) });
  const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(data));
  return [...new Uint8Array(hash)].slice(0, 12).map(b => b.toString(16).padStart(2, '0')).join('');
}

// --- Saved progress (IndexedDB) -----------------------------------------------------------
// Finished chunks are kept per book key, so closing the tab or losing the connection never
// costs work already done.

export function openStore(name = 'voxa-audiobooks') {
  return new Promise((ok, fail) => {
    const req = indexedDB.open(name, 1);
    req.onupgradeneeded = () => {
      req.result.createObjectStore('chunks');  // key: [book, chapter, index] -> ArrayBuffer of Int16
      req.result.createObjectStore('books');   // key: book -> {name, chapters, settings, saved}
    };
    req.onsuccess = () => ok(req.result);
    req.onerror = () => fail(req.error);
  });
}

function tx(db, store, mode, fn) {
  return new Promise((ok, fail) => {
    const t = db.transaction(store, mode), result = fn(t.objectStore(store));
    t.oncomplete = () => ok(result && 'result' in result ? result.result : undefined);
    t.onerror = () => fail(t.error); t.onabort = () => fail(t.error);
  });
}

export const saveChunk = (db, book, chapter, index, pcm) =>
  tx(db, 'chunks', 'readwrite', s => s.put(pcm.buffer.slice(pcm.byteOffset, pcm.byteOffset + pcm.byteLength), [book, chapter, index]));
export const loadChunk = async (db, book, chapter, index) => {
  const buf = await tx(db, 'chunks', 'readonly', s => s.get([book, chapter, index]));
  return buf ? new Int16Array(buf) : null;
};
export const savedChunkKeys = (db, book) =>
  tx(db, 'chunks', 'readonly', s => s.getAllKeys(IDBKeyRange.bound([book], [book, []])));
export const saveBook = (db, book, record) => tx(db, 'books', 'readwrite', s => s.put(record, book));
export const listBooks = db => tx(db, 'books', 'readonly', s => s.getAll());
export async function deleteBook(db, book) {
  await tx(db, 'chunks', 'readwrite', s => s.delete(IDBKeyRange.bound([book], [book, []])));
  await tx(db, 'books', 'readwrite', s => s.delete(book));
}

// --- Smaller files ------------------------------------------------------------------------
// MP3 is encoded on the device with lamejs (LGPL-3.0, served unmodified). A whole book is one
// MP3 whose ID3 tag carries chapter marks (CHAP/CTOC), which podcast and audiobook apps read.
// .m4b needs AAC, which browsers cannot all encode, so it is left to the server.

export const MP3_KBPS = 64;

/** Encode 16-bit mono samples to MP3 with a lamejs instance. */
export function encodeMp3(lame, pcm, rate = SAMPLE_RATE, kbps = MP3_KBPS, onProgress = () => {}) {
  const enc = new lame.Mp3Encoder(1, rate, kbps), parts = [];
  const BLOCK = 1152 * 20;
  for (let i = 0; i < pcm.length; i += BLOCK) {
    const out = enc.encodeBuffer(pcm.subarray(i, i + BLOCK));
    if (out.length) parts.push(new Uint8Array(out.buffer, out.byteOffset, out.length));
    if ((i / BLOCK) % 50 === 0) onProgress(i / pcm.length);
  }
  const tail = enc.flush();
  if (tail.length) parts.push(new Uint8Array(tail.buffer, tail.byteOffset, tail.length));
  return concatBytes(parts);
}

export function concatBytes(parts) {
  const out = new Uint8Array(parts.reduce((a, p) => a + p.length, 0));
  let at = 0;
  for (const p of parts) { out.set(p, at); at += p.length; }
  return out;
}

const MP3_RATES = { 3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000] };
const MP3_KBPS_L3 = { 3: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
                      2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160] };

/** Duration of an MP3 Layer III stream in seconds, by walking its frame headers. */
export function mp3Duration(bytes) {
  let at = 0, seconds = 0;
  if (bytes[0] === 0x49 && bytes[1] === 0x44 && bytes[2] === 0x33)  // skip an ID3v2 tag
    at = 10 + ((bytes[6] << 21) | (bytes[7] << 14) | (bytes[8] << 7) | bytes[9]);
  while (at + 4 <= bytes.length) {
    if (bytes[at] !== 0xff || (bytes[at + 1] & 0xe0) !== 0xe0) { at++; continue; }
    const version = (bytes[at + 1] >> 3) & 3, layer = (bytes[at + 1] >> 1) & 3;
    const kbps = (MP3_KBPS_L3[version] || MP3_KBPS_L3[2])[bytes[at + 2] >> 4];
    const rate = (MP3_RATES[version] || [])[(bytes[at + 2] >> 2) & 3];
    if (layer !== 1 || !kbps || !rate) { at++; continue; }
    const samples = version === 3 ? 1152 : 576;
    const length = Math.floor(samples / 8 * kbps * 1000 / rate) + ((bytes[at + 2] >> 1) & 1);
    seconds += samples / rate;
    at += length;
  }
  return seconds;
}

/** An ID3v2.3 tag with the book's title and a chapter mark per chapter. */
export function id3Chapters(title, chapters) {
  const enc = new TextEncoder();
  const u32 = n => [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255];
  // Text frames: encoding 1 (UTF-16 with BOM) so any language's titles survive.
  const text = s => { const b = [0xff, 0xfe]; for (const ch of s) { const c = ch.codePointAt(0); if (c > 0xffff) { const v = c - 0x10000, hi = 0xd800 + (v >> 10), lo = 0xdc00 + (v & 0x3ff); b.push(hi & 255, hi >> 8, lo & 255, lo >> 8); } else b.push(c & 255, c >> 8); } return [1, ...b, 0, 0]; };
  const frame = (id, body) => [...enc.encode(id), ...u32(body.length), 0, 0, ...body];
  const frames = [...frame('TIT2', text(title))];
  const ids = chapters.map((_, i) => `ch${i}`);
  frames.push(...frame('CTOC', [...enc.encode('toc'), 0, 0x03, ids.length, ...ids.flatMap(id => [...enc.encode(id), 0])]));
  chapters.forEach((c, i) => frames.push(...frame('CHAP', [
    ...enc.encode(ids[i]), 0, ...u32(Math.round(c.start * 1000)), ...u32(Math.round(c.end * 1000)),
    ...u32(0xffffffff), ...u32(0xffffffff), ...frame('TIT2', text(c.title))])));
  const size = frames.length;
  return new Uint8Array([...enc.encode('ID3'), 3, 0, 0, (size >> 21) & 127, (size >> 14) & 127, (size >> 7) & 127, size & 127, ...frames]);
}

/** One MP3 for the whole book: chapter MP3s back to back behind a chapter-marked ID3 tag. */
export function bookMp3(title, chapters) {
  let t = 0;
  const marks = chapters.map(c => { const d = mp3Duration(c.mp3); const m = { title: c.title, start: t, end: t + d }; t += d; return m; });
  return concatBytes([id3Chapters(title, marks), ...chapters.map(c => c.mp3)]);
}

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; }
  return t;
})();
export function crc32(bytes) {
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 255] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

/** A zip of files, stored without compression (MP3 does not compress further). */
export function zipStore(files) {
  const enc = new TextEncoder(), local = [], central = [];
  let offset = 0;
  const le = (n, size) => Array.from({ length: size }, (_, i) => (n >>> (8 * i)) & 255);
  for (const f of files) {
    const name = enc.encode(f.name), crc = crc32(f.data), n = f.data.length;
    // Version 20, flag 0x0800: names are UTF-8. Time/date fields fixed at 1980-01-01.
    const head = [...le(20, 2), ...le(0x0800, 2), ...le(0, 2), ...le(0, 2), ...le(0x21, 2), ...le(crc, 4), ...le(n, 4), ...le(n, 4), ...le(name.length, 2), ...le(0, 2)];
    local.push(new Uint8Array([...le(0x04034b50, 4), ...head, ...name]), f.data);
    central.push(new Uint8Array([...le(0x02014b50, 4), ...le(20, 2), ...head, ...le(0, 2), ...le(0, 2), ...le(0, 2), ...le(0, 4), ...le(offset, 4), ...name]));
    offset += 30 + name.length + n;
  }
  const dir = concatBytes(central);
  const end = new Uint8Array([...le(0x06054b50, 4), 0, 0, 0, 0, ...le(files.length, 2), ...le(files.length, 2), ...le(dir.length, 4), ...le(offset, 4), 0, 0]);
  return concatBytes([...local, dir, end]);
}
