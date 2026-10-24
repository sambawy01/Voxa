// Text handling for Hojo TTS Light, Voxa's on-device engine for its own English voices and
// Chinese: spelling numbers out (the model reads raw digits badly), splitting long text into
// pieces it can say in one go, and the tokenizer. Pure functions, so they run in a worker
// and in Node tests.

const ONES = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten',
  'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen', 'seventeen', 'eighteen', 'nineteen'];
const TENS = ['', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety'];
const SCALES = [[1e12, 'trillion'], [1e9, 'billion'], [1e6, 'million'], [1e3, 'thousand']];

function under1000(n) {
  const out = [];
  if (n >= 100) { out.push(ONES[Math.floor(n / 100)], 'hundred'); n %= 100; }
  if (n >= 20) out.push(TENS[Math.floor(n / 10)] + (n % 10 ? '-' + ONES[n % 10] : ''));
  else if (n || !out.length) out.push(ONES[n]);
  return out.join(' ');
}

/** 1234 -> "one thousand two hundred thirty-four"; digit by digit past a trillion. */
export function englishNumber(n) {
  if (!Number.isSafeInteger(n) || n >= 1e15) return String(n).split('').map(d => ONES[+d] || d).join(' ');
  if (n < 1000) return under1000(n);
  const out = [];
  for (const [size, word] of SCALES) {
    if (n >= size) { out.push(englishNumber(Math.floor(n / size)), word); n %= size; }
  }
  if (n) out.push(under1000(n));
  return out.join(' ');
}

/** Years read in pairs, as people say them: 1984 -> "nineteen eighty-four", 2005 -> "two thousand five". */
export function englishYear(n) {
  if (n >= 2000 && n < 2010) return englishNumber(n);
  const hi = Math.floor(n / 100), lo = n % 100;
  return under1000(hi) + ' ' + (lo === 0 ? 'hundred' : lo < 10 ? 'oh ' + ONES[lo] : under1000(lo));
}

const ORDINAL = { one: 'first', two: 'second', three: 'third', five: 'fifth', eight: 'eighth', nine: 'ninth', twelve: 'twelfth' };
function englishOrdinal(n) {
  const words = englishNumber(n);
  return words.replace(/([a-z]+)$/, w => ORDINAL[w] || (w.endsWith('y') ? w.slice(0, -1) + 'ieth' : w + 'th'));
}

const digitsOf = s => s.split('').map(d => ONES[+d]).join(' ');
const CURRENCY = { '$': ['dollar', 'dollars', 'cent', 'cents'], '£': ['pound', 'pounds', 'penny', 'pence'],
  '€': ['euro', 'euros', 'cent', 'cents'], '¥': ['yen', 'yen', '', ''] };

function englishAmount(whole, frac) {
  const n = Number(whole.replace(/,/g, ''));
  return frac === undefined ? englishNumber(n) : englishNumber(n) + ' point ' + digitsOf(frac);
}

/** Spell out numbers, money, percentages, times and ordinals in English text. */
export function normalizeEnglish(text) {
  return text
    .replace(/([$£€¥])\s?(\d[\d,]*)(?:\.(\d+))?(\s?(?:million|billion|trillion|thousand)\b)?/g, (m, sym, whole, frac, scale) => {
      const [one, many, sub, subs] = CURRENCY[sym], n = Number(whole.replace(/,/g, ''));
      if (scale) return englishAmount(whole, frac) + scale + ' ' + many;
      let out = englishNumber(n) + ' ' + (n === 1 ? one : many);
      if (frac && sub && /^\d{1,2}$/.test(frac)) {
        const c = Number(frac.padEnd(2, '0'));
        if (c) out += ' ' + englishNumber(c) + ' ' + (c === 1 ? sub : subs);
      } else if (frac) out = englishAmount(whole, frac) + ' ' + many;
      return out;
    })
    // "a.m." ends a sentence when a capital or the end of the text follows; keep that stop.
    .replace(/\b(\d{1,2})(?::(\d{2}))?\s?([ap])(\.m\.|m\b)/gi,
      (m, h, mm, ap, form, at, all) => (mm ? clock(h, mm) : englishNumber(+h)) + ' ' + ap.toLowerCase() + ' m' +
        (form.length > 1 && /^(\s+[A-Z]|\s*$)/.test(all.slice(at + m.length)) ? '.' : ''))
    .replace(/\b(\d{1,2}):(\d{2})\b/g, (m, h, mm) => clock(h, mm))
    .replace(/(^|[^\d.,])-(\d)/g, '$1minus $2')
    .replace(/(\d[\d,]*)(?:\.(\d+))?\s?%/g, (m, w, f) => englishAmount(w, f) + ' percent')
    .replace(/\b(\d+)(st|nd|rd|th)\b/gi, (m, n) => englishOrdinal(Number(n)))
    .replace(/\b(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\b/g, (m, w, f) => {
      const plain = w.replace(/,/g, ''), n = Number(plain);
      if (f === undefined && !w.includes(',') && plain.length === 4 && n >= 1100 && n < 2100) return englishYear(n);
      if (plain.length > 1 && plain.startsWith('0')) return digitsOf(plain) + (f ? ' point ' + digitsOf(f) : '');
      return englishAmount(w, f);
    })
    .replace(/&/g, ' and ');
}

function clock(h, mm) {
  const m = Number(mm);
  return englishNumber(Number(h)) + (m === 0 ? " o'clock" : m < 10 ? ' oh ' + ONES[m] : ' ' + englishNumber(m));
}

const ZH_DIGITS = '零一二三四五六七八九';
const ZH_UNITS = ['', '十', '百', '千'];

function zhUnder10000(n, leadingZero) {
  let out = '', zero = false;
  for (let i = 3; i >= 0; i--) {
    const d = Math.floor(n / 10 ** i) % 10;
    if (d) { if (zero || (leadingZero && !out && i < 3)) out += '零'; out += ZH_DIGITS[d] + ZH_UNITS[i]; zero = false; }
    else if (out) zero = true;
  }
  return out;
}

/** 2024 -> 两千零二十四 style readings (with 二), 10 -> 十, 15 -> 十五, 100000 -> 十万. */
export function chineseNumber(n) {
  if (!Number.isSafeInteger(n) || n >= 1e16) return String(n).split('').map(d => ZH_DIGITS[+d] ?? d).join('');
  if (n === 0) return '零';
  const groups = [];
  for (let x = n; x > 0; x = Math.floor(x / 10000)) groups.push(x % 10000);
  const units = ['', '万', '亿', '万亿'];
  let out = '';
  for (let i = groups.length - 1; i >= 0; i--) {
    const g = groups[i];
    if (!g) continue;
    out += zhUnder10000(g, !!out && g < 1000) + units[i];
  }
  // 一十五 is said 十五 at the start of a number.
  return out.replace(/^一十/, '十');
}

const zhDigits = s => s.split('').map(d => ZH_DIGITS[+d]).join('');

/** Spell out numbers, dates, percentages and times in Chinese text. */
export function normalizeChinese(text) {
  return text
    .replace(/(\d{4})\s?年/g, (m, y) => zhDigits(y) + '年')
    .replace(/(\d{1,2}):(\d{2})/g, (m, h, mm) => chineseNumber(+h) + '点' + (+mm ? chineseNumber(+mm) + '分' : '整'))
    .replace(/(^|[^\d.])-(\d)/g, '$1负$2')
    .replace(/(\d[\d,]*)(?:\.(\d+))?\s?[%％]/g, (m, w, f) => '百分之' + chineseNumber(Number(w.replace(/,/g, ''))) + (f ? '点' + zhDigits(f) : ''))
    .replace(/(?:°C|℃)/g, '摄氏度')
    .replace(/(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?/g, (m, w, f) => {
      const plain = w.replace(/,/g, '');
      if (plain.length > 1 && (plain.startsWith('0') || plain.length >= 11)) return zhDigits(plain);  // phone numbers, codes
      return chineseNumber(Number(plain)) + (f ? '点' + zhDigits(f) : '');
    });
}

/** Normalize for a voice language: 'z' (Chinese) or anything else (English). */
export function normalizeForHojo(text, lang) {
  // Brackets would read as the model's own control tokens; they are never spoken anyway.
  const clean = text.replace(/[[\]<>{}]/g, ' ').replace(/[“”]/g, '"').replace(/[‘’]/g, "'");
  return (lang === 'z' ? normalizeChinese(clean) : normalizeEnglish(clean)).replace(/\s+/g, ' ').trim();
}

/**
 * Split text into pieces short enough for one generation: whole sentences, joined while they
 * fit in `max` characters, and long sentences broken at commas, then at spaces.
 */
export function splitForHojo(text, lang) {
  const max = lang === 'z' ? 80 : 220;
  const sentences = text.match(/[^.!?。！？；;]+(?:[.!?。！？；;]+["'”’）)]*|$)\s*/gu) || [];
  const pieces = [];
  for (const raw of sentences) {
    const s = raw.trim();
    if (!s) continue;
    if (s.length <= max) { pieces.push(s); continue; }
    let buf = '';
    for (const part of s.split(/(?<=[,，、:：])\s*/u)) {
      for (const bit of part.length <= max ? [part] : wrap(part, max)) {
        if (buf && (buf + ' ' + bit).length > max) { pieces.push(buf); buf = bit; }
        else buf = buf ? buf + (lang === 'z' ? '' : ' ') + bit : bit;
      }
    }
    if (buf) pieces.push(buf);
  }
  // Join short neighbours so each generation has some context.
  const out = [];
  for (const p of pieces) {
    const last = out[out.length - 1], sep = lang === 'z' ? '' : ' ';
    if (last && (last + sep + p).length <= max) out[out.length - 1] = last + sep + p;
    else out.push(p);
  }
  return out.filter(p => /[\p{L}\p{N}]/u.test(p));
}

function wrap(text, max) {
  const words = text.includes(' ') ? text.split(' ') : Array.from(text);
  const sep = text.includes(' ') ? ' ' : '', out = [];
  let buf = '';
  for (const w of words) {
    if (buf && (buf + sep + w).length > max) { out.push(buf); buf = w; }
    else buf = buf ? buf + sep + w : w;
  }
  if (buf) out.push(buf);
  return out;
}

const WORD = /\s+|[\p{L}\p{M}\p{N}\p{Pc}]+|[^\p{L}\p{M}\p{N}\p{Pc}\s]+/gu;

/**
 * The tokenizer Hojo was trained with (Hugging Face tokenizers, as described in hojo.json):
 * lowercase, collapse whitespace, every digit on its own, split into runs of word characters,
 * spaces and other symbols, then byte-pair merges within each run. Unknown characters become
 * the unknown token, as they do there.
 */
export class HojoTokenizer {
  constructor(meta) {
    this.vocab = meta.vocabulary;
    this.unk = meta.unk;
    this.ranks = new Map(meta.merges.map((m, i) => [m, i]));
  }

  bpe(piece) {
    let parts = Array.from(piece);
    while (parts.length > 1) {
      let best = -1, rank = Infinity;
      for (let i = 0; i < parts.length - 1; i++) {
        const r = this.ranks.get(parts[i] + ' ' + parts[i + 1]);
        if (r !== undefined && r < rank) { rank = r; best = i; }
      }
      if (best < 0) break;
      parts = [...parts.slice(0, best), parts[best] + parts[best + 1], ...parts.slice(best + 2)];
    }
    return parts;
  }

  encode(text) {
    const norm = text.toLowerCase().replace(/\s+/gu, ' ');
    const ids = [];
    for (const chunk of norm.split(/(\p{Nd})/u)) {
      if (!chunk) continue;
      for (const piece of chunk.match(WORD) || []) {
        for (const tok of this.bpe(piece)) ids.push(this.vocab[tok] ?? this.unk);
      }
    }
    return ids;
  }
}

/** The prompt Hojo expects: the text, sixteen speaker slots, then the start of speech. */
export function promptIds(tokenizer, meta, text) {
  const s = meta.special;
  return [s['[target_text_start]'], ...tokenizer.encode(text), s['[target_text_end]'],
    s['[spk_start]'], ...meta.speaker_slots, s['[spk_end]'], s['[target_speech_start]']];
}
