// Arabic numbers: digits read as words.
//
// The Arabic voices read "1998" or "01012345678" as noise, but read the same numbers written as
// words perfectly. So digits become words before the voice sees them: cardinals that agree with
// the counted noun (ثلاث سنوات, ثلاثة كتب), years and amounts, phone numbers digit by digit in
// groups, times, percentages, decimals, dates and ranges.
//
// agentapi.py (the local server) does the same in Python; tests/arabic_numbers_cases.json holds
// the cases both must pass, so the two cannot drift apart.

const UM = ['صفر', 'واحد', 'اثنين', 'ثلاثة', 'أربعة', 'خمسة', 'ستة', 'سبعة', 'ثمانية', 'تسعة', 'عشرة'];
const UF = ['صفر', 'واحدة', 'اثنتين', 'ثلاث', 'أربع', 'خمس', 'ست', 'سبع', 'ثمان', 'تسع', 'عشر'];
const TM = { 11: 'أحد عشر', 12: 'اثني عشر', 13: 'ثلاثة عشر', 14: 'أربعة عشر', 15: 'خمسة عشر', 16: 'ستة عشر', 17: 'سبعة عشر', 18: 'ثمانية عشر', 19: 'تسعة عشر' };
const TF = { 11: 'إحدى عشرة', 12: 'اثنتي عشرة', 13: 'ثلاث عشرة', 14: 'أربع عشرة', 15: 'خمس عشرة', 16: 'ست عشرة', 17: 'سبع عشرة', 18: 'ثماني عشرة', 19: 'تسع عشرة' };
const TENS = { 2: 'عشرين', 3: 'ثلاثين', 4: 'أربعين', 5: 'خمسين', 6: 'ستين', 7: 'سبعين', 8: 'ثمانين', 9: 'تسعين' };
const HUND = { 1: 'مائة', 2: 'مائتين', 3: 'ثلاثمائة', 4: 'أربعمائة', 5: 'خمسمائة', 6: 'ستمائة', 7: 'سبعمائة', 8: 'ثمانمائة', 9: 'تسعمائة' };
const SCALES = [[1e9, 'مليار', 'مليارين', 'مليارات'], [1e6, 'مليون', 'مليونين', 'ملايين'], [1000, 'ألف', 'ألفين', 'آلاف']];
const HOUR = { 1: 'الأولى', 2: 'الثانية', 3: 'الثالثة', 4: 'الرابعة', 5: 'الخامسة', 6: 'السادسة', 7: 'السابعة', 8: 'الثامنة', 9: 'التاسعة', 10: 'العاشرة', 11: 'الحادية عشرة', 12: 'الثانية عشرة' };
// Feminine counted nouns the endings do not show (and masculine plurals that end in ات).
const FEM = new Set('دقائق ثوان ثواني ليالي سنين دول مدن نساء أوراق رسائل أفكار قطع نقاط عيون أيدي أعين أم أخت بنت يد عين دار نار شمس أرض حرب'.split(' '));
const MASC = new Set('ملفات أوقات تطبيقات فيديوهات مشروبات مستندات حسابات كيلومترات سنتيمترات ملليمترات دولارات جنيهات ريالات جرامات كيلوجرامات لترات أمتار'.split(' '));
const DIGIT_MAP = { '٠': '0', '١': '1', '٢': '2', '٣': '3', '٤': '4', '٥': '5', '٦': '6', '٧': '7', '٨': '8', '٩': '9',
  '۰': '0', '۱': '1', '۲': '2', '۳': '3', '۴': '4', '۵': '5', '۶': '6', '۷': '7', '۸': '8', '۹': '9', '٫': '.', '٬': ',' };
const RUN = /(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\+?\d{2,4}(?:[ \-]\d{2,4}){2,}|\+?\d+(?:[.:/\-]\d+)*)(\s*%)?/g;
const AR = '[\\u0621-\\u064A\\u0671-\\u06D3]+';

function under1000(n, fem) {
  const parts = [], h = Math.floor(n / 100), r = n % 100;
  if (h) parts.push(HUND[h]);
  if (r) {
    if (r <= 10) parts.push((fem ? UF : UM)[r]);
    else if (r < 20) parts.push((fem ? TF : TM)[r]);
    else {
      const t = Math.floor(r / 10), u = r % 10;
      if (u) parts.push((u === 1 && fem ? 'إحدى' : (fem ? UF : UM)[u]) + ' و' + TENS[t]);
      else parts.push(TENS[t]);
    }
  }
  return parts.join(' و');
}

/** 1998 -> ألف وتسعمائة وثمانية وتسعون. `fem`: the counted noun is feminine (ثلاث سنوات). */
export function cardinal(n, fem = false) {
  if (n === 0) return 'صفر';
  const parts = []; let rest = n;
  for (const [size, one, two, few] of SCALES) {
    const q = Math.floor(rest / size); rest %= size;
    if (!q) continue;
    if (q === 1) parts.push(one);
    else if (q === 2) parts.push(two);
    else if (q === 200) parts.push('مائتي ' + one);
    else parts.push(under1000(q, false) + ' ' + (q % 100 >= 3 && q % 100 <= 10 ? few : one));
  }
  if (rest) parts.push(under1000(rest, fem));
  return parts.join(' و');
}

function isFem(word) {
  let w = (word || '').replace(/[ً-ٰٟـ]/g, '').replace(/^(?:[وفبلك]?ال|لل)/, '');
  if (FEM.has(w)) return true;
  if (MASC.has(w)) return false;
  return w.endsWith('ة') || w.endsWith('ات');
}

const digitsAsWords = (s, sep = ' ') => [...s].map(c => UM[+c]).join(sep);

function phoneGroups(digits, raw) {
  const groups = raw.match(/\d+/g) || [];
  if (groups.length >= 3 && groups.reduce((a, g) => a + g.length, 0) === digits.length && !raw.startsWith('+')) return groups;
  if (raw.startsWith('+') && groups.length >= 3) return groups;
  if (/^01[0125]\d{8}$/.test(digits)) return [digits.slice(0, 3), digits.slice(3, 7), digits.slice(7)];
  const out = [];
  for (let i = 0; i < digits.length; i += 3) out.push(digits.slice(i, i + 3));
  if (out.length > 1 && out[out.length - 1].length === 1) out[out.length - 2] += out.pop();
  return out;
}

function isPhone(run) {
  const digits = run.replace(/\D/g, '');
  if (digits.length < 8) return false;
  if (run.startsWith('+') || digits.startsWith('0')) return true;
  const groups = run.match(/\d+/g) || [];
  return /^[\d ]+$/.test(run) && ((groups.length === 1 && digits.length >= 9) || (groups.length >= 3 && groups.every(g => g.length >= 3 && g.length <= 4)));
}

const phone = run => (run.startsWith('+') ? 'زائد ' : '') +
  phoneGroups(run.replace(/\D/g, ''), run).map(g => digitsAsWords(g)).join('، ');

function time(h, m) {
  const hh = h % 12 || 12;
  if (m === 0) return HOUR[hh];
  if (m === 15) return HOUR[hh] + ' وربع';
  if (m === 30) return HOUR[hh] + ' ونصف';
  if (m === 45) return HOUR[hh % 12 + 1] + ' إلا ربع';
  let mins;
  if (m === 1) mins = 'دقيقة واحدة';
  else if (m === 2) mins = 'دقيقتان';
  else if (m <= 10) mins = cardinal(m, true) + ' دقائق';
  else mins = cardinal(m, true) + ' دقيقة';
  return HOUR[hh] + ' و' + mins;
}

function int(s, fem) {
  if (s.length > 1 && s[0] === '0') return digitsAsWords(s);          // codes such as 007
  const n = Number(s);
  if (n >= 1e12) { const out = []; for (let i = 0; i < s.length; i += 3) out.push(digitsAsWords(s.slice(i, i + 3))); return out.join('، '); }
  return cardinal(n, fem);
}

function compact(c, fem, hour) {
  let m = /^(\d{1,2}):(\d{2})$/.exec(c);
  if (m) {
    const h = +m[1], mi = +m[2];
    return h <= 24 && mi <= 59 ? time(h, mi) : int(m[1], false) + '، ' + int(m[2], false);
  }
  m = /^(?:(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})|(\d{4})-(\d{1,2})-(\d{1,2}))$/.exec(c);
  if (m) {
    let g = m.slice(1).filter(x => x !== undefined);
    if (m[4] !== undefined) g = [g[2], g[1], g[0]];                       // 2024-05-12 -> day, month, year
    return g.map(x => int(String(+x), false)).join('، ');
  }
  if (/^\d+(?:\.\d+){2,}$/.test(c)) return c.split('.').map(x => int(x, false)).join(' نقطة ');
  m = /^(\d+)\.(\d+)$/.exec(c);
  if (m) {
    const frac = m[2];
    return int(m[1], false) + ' فاصلة ' + (frac.length <= 3 && frac[0] !== '0' ? int(frac, false) : digitsAsWords(frac));
  }
  m = /^(\d+)-(\d+)$/.exec(c);
  if (m) {
    return int(m[1], fem) + ' إلى ' + int(m[2], fem);
  }
  m = /^(\d+)\/(\d+)$/.exec(c);
  if (m) return int(m[1], false) + ' على ' + int(m[2], false);
  if (/^\d+$/.test(c)) return hour && +c >= 1 && +c <= 12 ? HOUR[+c] : int(c, fem);
  return (c.match(/\d+/g) || []).map(x => int(x, fem)).join('، ');
}

/** Arabic text with its digits written as words, for the voice. */
export function normalizeArabic(text) {
  if (!text || !/[0-9٠-٩۰-۹]/.test(text)) return text;
  const t = text.replace(/[٠-٩۰-۹٫٬]/g, ch => DIGIT_MAP[ch]);
  let out = '', last = 0;
  RUN.lastIndex = 0;
  let m;
  while ((m = RUN.exec(t))) {
    const a = m.index, b = a + m[0].length;
    if (/[A-Za-z_][\-./:]?$/.test(t.slice(0, a)) || /^[\-./:]?[A-Za-z_]/.test(t.slice(b))) continue;   // mp3, A4, COVID-19, 5G
    const run = m[1], pct = !!m[2];
    const nxt = new RegExp('^\\s*(' + AR + ')').exec(t.slice(b));
    const fem = nxt ? isFem(nxt[1]) : false;
    const prev = new RegExp('(' + AR + ')\\s*$').exec(t.slice(0, a));
    const hour = !!prev && ['الساعة', 'الساعه'].includes(prev[1].replace(/[ً-ٟ]/g, ''));
    let words;
    if (run.includes(',')) words = compact(run.replace(/,/g, ''), fem, hour);          // 15,000: an amount, never a phone number
    else if (isPhone(run)) words = phone(run);
    else if (run.includes(' ') && !/^\d{4}-\d{1,2}-\d{1,2}$/.test(run)) words = run.split(' ').map(x => compact(x, fem, hour)).join(' ');
    else words = compact(run, fem, hour);
    if (pct) words += ' بالمئة';
    out += t.slice(last, a) + words;
    last = b;
  }
  return out + t.slice(last);
}
