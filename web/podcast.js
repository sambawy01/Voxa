// Voxa podcasts: read a document on the device, then turn the written script into speech parts.
// Pure functions where possible, so they can be tested without a browser (tests/podcast.test.mjs).
import { linesFromItems, cleanPages, paragraphsFromLines, splitChunks, concatBytes } from './book.js';

// Characters sent for a script; the server allows 200,000. Longer documents are cut, with a note.
export const MAX_TEXT = 200000;
export const TURN_PAUSE = 0.35;   // seconds of silence between speakers

/** Text of a PDF, page by page, with page markers so the hosts can follow a deck's order. */
export async function pdfText(pdfjs, data, onProgress = () => {}, options = {}) {
  const doc = await pdfjs.getDocument({ ...options, data, isEvalSupported: false }).promise;
  const pages = [];
  for (let n = 1; n <= doc.numPages; n++) {
    const page = await doc.getPage(n);
    pages.push(linesFromItems((await page.getTextContent()).items));
    onProgress(n, doc.numPages);
  }
  return { pages: doc.numPages, text: pagesToText(cleanPages(pages), 'Page') };
}

export function pagesToText(pages, label) {
  return pages.map((lines, i) => {
    const body = Array.isArray(lines) ? paragraphsFromLines(lines).join('\n') : lines;
    return body.trim() ? `[${label} ${i + 1}]\n${body.trim()}` : '';
  }).filter(Boolean).join('\n\n');
}

// --- PowerPoint (.pptx) ----------------------------------------------------------------------
// A .pptx is a zip of XML files. Slides are ppt/slides/slideN.xml; speaker notes, often the
// most useful text in a deck, are linked from each slide's relationships file.

/** Files of a zip archive: Map name -> async () => Uint8Array. Handles stored and deflated entries. */
export function readZip(bytes) {
  const v = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let end = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 65557); i--) {
    if (v.getUint32(i, true) === 0x06054b50) { end = i; break; }
  }
  if (end < 0) throw new Error('not a zip file');
  const count = v.getUint16(end + 10, true);
  let at = v.getUint32(end + 16, true);
  const files = new Map(), dec = new TextDecoder();
  for (let n = 0; n < count; n++) {
    if (v.getUint32(at, true) !== 0x02014b50) throw new Error('damaged zip file');
    const method = v.getUint16(at + 10, true), size = v.getUint32(at + 20, true);
    const nameLen = v.getUint16(at + 28, true), extra = v.getUint16(at + 30, true), comment = v.getUint16(at + 32, true);
    const local = v.getUint32(at + 42, true);
    const name = dec.decode(bytes.subarray(at + 46, at + 46 + nameLen));
    files.set(name, async () => {
      const start = local + 30 + v.getUint16(local + 26, true) + v.getUint16(local + 28, true);
      const raw = bytes.subarray(start, start + size);
      if (method === 0) return raw;
      if (method !== 8) throw new Error('unsupported zip compression');
      const stream = new Blob([raw]).stream().pipeThrough(new DecompressionStream('deflate-raw'));
      return new Uint8Array(await new Response(stream).arrayBuffer());
    });
    at += 46 + nameLen + extra + comment;
  }
  return files;
}

const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" };
export const unescapeXml = s => s.replace(/&(#x[0-9a-f]+|#[0-9]+|[a-z]+);/gi, (m, e) =>
  e[0] === '#' ? String.fromCodePoint(parseInt(e[1] === 'x' || e[1] === 'X' ? e.slice(2) : e.slice(1), e[1] === 'x' || e[1] === 'X' ? 16 : 10)) : ENTITIES[e] ?? m);

/** Paragraphs of DrawingML text (<a:p> containing <a:t> runs). */
export function drawingText(xml) {
  const paras = [];
  for (const p of xml.match(/<a:p[ >][\s\S]*?<\/a:p>/g) || []) {
    const text = (p.match(/<a:t(?: [^>]*)?>[\s\S]*?<\/a:t>|<a:br\s*\/>/g) || [])
      .map(t => t.startsWith('<a:br') ? ' ' : unescapeXml(t.replace(/^<a:t[^>]*>|<\/a:t>$/g, ''))).join('');
    if (text.trim()) paras.push(text.replace(/\s+/g, ' ').trim());
  }
  return paras;
}

/** Text of a .pptx: each slide's text and its speaker notes, in slide order. */
export async function pptxText(bytes) {
  const files = readZip(bytes), dec = new TextDecoder();
  const read = async name => files.has(name) ? dec.decode(await files.get(name)()) : '';
  const slides = [...files.keys()].map(n => n.match(/^ppt\/slides\/slide(\d+)\.xml$/)).filter(Boolean)
    .sort((a, b) => a[1] - b[1]).map(m => m[0]);
  // Slide order in the presentation can differ from file numbers; presentation.xml has the real order.
  const order = await slideOrder(read);
  if (order.length) slides.sort((a, b) => (order.indexOf(a) + 1 || 1e9) - (order.indexOf(b) + 1 || 1e9));
  const pages = [];
  for (const slide of slides) {
    const body = drawingText(await read(slide));
    const rels = await read(slide.replace('slides/', 'slides/_rels/') + '.rels');
    const target = (rels.match(/Target="\.\.\/notesSlides\/(notesSlide\d+\.xml)"/) || [])[1];
    // Notes slides repeat the slide number as a field; drop bare numbers.
    const notes = target ? drawingText(await read('ppt/notesSlides/' + target)).filter(t => !/^\d+$/.test(t)) : [];
    pages.push(body.join('\n') + (notes.length ? '\nSpeaker notes: ' + notes.join(' ') : ''));
  }
  return { pages: slides.length, text: pagesToText(pages, 'Slide') };
}

async function slideOrder(read) {
  const pres = await read('ppt/presentation.xml'), rels = await read('ppt/_rels/presentation.xml.rels');
  const targets = {};
  for (const m of rels.matchAll(/<Relationship [^>]*>/g)) {
    const id = (m[0].match(/Id="([^"]+)"/) || [])[1], t = (m[0].match(/Target="([^"]+)"/) || [])[1];
    if (id && t) targets[id] = 'ppt/' + t.replace(/^\/?ppt\//, '');
  }
  return [...pres.matchAll(/<p:sldId [^>]*r:id="([^"]+)"/g)].map(m => targets[m[1]]).filter(Boolean);
}

/** Cut text to the limit at a paragraph boundary. Returns {text, cut}. */
export function limitText(text, max = MAX_TEXT) {
  if (text.length <= max) return { text, cut: false };
  const at = text.lastIndexOf('\n\n', max);
  return { text: text.slice(0, at > max * 0.8 ? at : max), cut: true };
}

// --- Editing the script ------------------------------------------------------------------------
// Each chapter is edited as plain text, one turn per paragraph: "Name: what they say".

export function chapterToText(chapter, hosts) {
  return chapter.lines.map(l => `${hosts[l.speaker]}: ${l.text}`).join('\n\n');
}

/** Turns from edited text. A paragraph without a host's name continues the previous turn. */
export function textToLines(text, hosts) {
  const names = Object.entries(hosts).map(([speaker, name]) => ({ speaker, name: name.toLowerCase() }))
    .sort((a, b) => b.name.length - a.name.length);
  const lines = [];
  for (const para of text.split(/\n\s*\n|\n(?=[^\n:]{1,40}:)/).map(s => s.trim()).filter(Boolean)) {
    const m = para.match(/^([^:\n]{1,40}):\s*([\s\S]*)$/);
    const who = m && names.find(n => n.name === m[1].trim().toLowerCase());
    // A name on its own starts a turn that the next paragraph fills.
    if (who) lines.push({ speaker: who.speaker, text: m[2].replace(/\s+/g, ' ').trim() });
    else if (lines.length) lines[lines.length - 1].text = (lines[lines.length - 1].text + ' ' + para.replace(/\s+/g, ' ')).trim();
    else lines.push({ speaker: 'A', text: para.replace(/\s+/g, ' ') });
  }
  return lines.filter(l => l.text);
}

export const scriptChars = script => script.chapters.reduce((a, c) => a + c.lines.reduce((b, l) => b + l.text.length, 0), 0);

/** Every speech part, in order: [{chapter, index, speaker, text}]. Long turns are split. */
export function planParts(script, max = 1500) {
  const plan = [];
  script.chapters.forEach((c, chapter) => {
    let index = 0;
    for (const l of c.lines) for (const text of splitChunks(l.text, max)) plan.push({ chapter, index: index++, speaker: l.speaker, text });
  });
  return plan;
}

/** One chapter's audio: parts joined with a short pause, longer where the speaker changes. */
export function joinTurns(parts, speakers, rate = 24000) {
  const gap = i => i === 0 ? 0 : Math.round(rate * (speakers[i] === speakers[i - 1] ? 0.15 : TURN_PAUSE));
  const total = parts.reduce((a, p, i) => a + gap(i) + p.length, 0);
  const out = new Int16Array(total);
  let at = 0;
  parts.forEach((p, i) => { at += gap(i); out.set(p, at); at += p.length; });
  return out;
}

export { concatBytes };
