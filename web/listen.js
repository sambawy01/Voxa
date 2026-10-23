// Voxa Reader: read any copied or shared text aloud, part by part.
// Pure functions, tested in tests/listen.test.mjs.
import { paragraphsFromText, splitChunks } from './book.js';

export const FIRST_PART = 240;  // characters: a short first part, so audio starts within seconds
export const PART = 700;        // later parts: about 45 seconds of speech each

/** Copied text, made fit to read aloud: no quoted replies, no raw links, tidy spacing. */
export function cleanForSpeech(text) {
  return String(text || '').replace(/\r\n?/g, '\n')
    .split('\n')
    .filter(l => !/^\s*>/.test(l))                               // quoted lines of a reply
    .filter(l => !/^\s*On .{4,160} wrote:\s*$/.test(l))          // "On Mon, … wrote:"
    .join('\n')
    .replace(/https?:\/\/[^\s)>\]]+/g, 'link')                   // nobody wants a URL read out
    .replace(/[ \t ]+/g, ' ')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

/** Paragraphs to show, and the parts to speak: [{para, text}], the first one short. */
export function readerPlan(text) {
  const paras = paragraphsFromText(cleanForSpeech(text));
  const parts = [];
  paras.forEach((p, para) => {
    let pieces = splitChunks(p, PART);
    if (!parts.length && pieces.length) {
      const opening = splitChunks(pieces[0], FIRST_PART);
      const rest = [opening.slice(1).join(' '), ...pieces.slice(1)].filter(Boolean).join(' ');
      pieces = [opening[0], ...(rest ? splitChunks(rest, PART) : [])];
    }
    for (const t of pieces) parts.push({ para, text: t.replace(/\s*\n\s*/g, ' ') });
  });
  return { paras, parts };
}

// Common words per language, matched as whole words (accented letters count as letters).
const words = (list, extra = '') => new RegExp(`(?<!\\p{L})(${list})(?!\\p{L})${extra}`, 'giu');
const WORDS = {
  e: words('el|la|los|las|que|de|y|en|por|para|una|está|gracias|hola', '|ñ|¿|¡'),
  f: words('le|les|des|est|et|je|vous|nous|une|pour|avec|merci|bonjour', '|ç|è|ê'),
  i: words('il|che|di|per|non|sono|una|grazie|ciao|questo|della'),
  p: words('não|você|para|uma|obrigado|obrigada|olá|com|isso|está', '|ção|ã|õ'),
  a: words('the|and|is|you|to|of|for|with|this|that|thanks|hi'),
};

/**
 * The language to read in: scripts decide it outright; otherwise common words, falling back
 * to `fallback` (the reader's last choice). Returns a Voxa language code, or 'unsupported'.
 */
export function detectLang(text, fallback = 'a') {
  const sample = String(text || '').slice(0, 4000);
  const count = re => (sample.match(re) || []).length;
  const letters = count(/\p{L}/gu) || 1;
  if (count(/[؀-ۿݐ-ݿ]/g) / letters > 0.3) return 'unsupported';  // Arabic script
  if (count(/[぀-ヿ]/g) / letters > 0.05) return 'j';
  if (count(/[一-鿿]/g) / letters > 0.3) return 'z';
  if (count(/[ऀ-ॿ]/g) / letters > 0.3) return 'h';
  const scores = Object.fromEntries(Object.entries(WORDS).map(([k, re]) => [k, count(re)]));
  const [best, n] = Object.entries(scores).sort((a, b) => b[1] - a[1])[0];
  if (n < 3) return fallback;
  if (best === 'a') return fallback === 'b' ? 'b' : 'a';
  return best;
}

/** A Voxa Reader link carrying the text in its #fragment, which browsers never send to servers. */
export const readerLink = (origin, text) => `${origin}/listen#t=${encodeURIComponent(text)}`;

/** Text handed to the reader: #t= (Shortcuts, bookmarklet) or ?text=/title=/url= (share target). */
export function incomingText(href) {
  const u = new URL(href);
  const hash = new URLSearchParams(u.hash.slice(1));
  if (hash.get('t')) return hash.get('t');
  const q = u.searchParams;
  return [q.get('title'), q.get('text'), q.get('url')].filter(Boolean).join('\n\n');
}

/** Seconds as "4 min" / "45 s", for the time left. */
export function shortDuration(seconds) {
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))} s`;
  return `${Math.round(seconds / 60)} min`;
}
