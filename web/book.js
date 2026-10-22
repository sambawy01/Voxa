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
