// Text handling for tiny-voca, shared by its worker (a classic script, loaded with
// importScripts) and by the tests (require). One call of the model reads one utterance of up
// to 512 phoneme tokens (about 32 seconds), and each must end with a mark such as a full stop
// or its last word breaks off. This follows the splitting in the model authors' own code
// (tiny_voca/infer.py, Apache-2.0).
(function (root) {
  const ABBREVIATIONS = /(?:\b(?:mr|mrs|ms|dr|prof|sr|jr|st|vs|etc|no|inc|ltd|co|e\.g|i\.e|a\.m|p\.m)|\b[A-Z])\.$/i;
  const SENTENCE_END = /(?<=[.!?…]["')\]]?)\s+(?=["'(\[]?[A-Z0-9])/;
  const CLAUSE_END = /(?<=[,;:—])\s+/;
  const CLOSING = /["'”’)\]»]+$/;
  const PEAK = 0.99;               // the model can peak a little above 1.0

  /** The text as sentences, without splitting after an abbreviation such as "Dr." */
  function sentences(text) {
    const parts = [];
    let current = '';
    for (const piece of text.replace(/[\s﻿]+/g, ' ').trim().split(SENTENCE_END)) {
      current = current ? current + ' ' + piece : piece;
      if (!ABBREVIATIONS.test(current)) { parts.push(current); current = ''; }
    }
    if (current) parts.push(current);
    return parts.filter(part => /[\p{L}\p{N}]/u.test(part));
  }

  /** The text with a closing mark, adding a full stop when it ends without one. */
  function finished(text) {
    const body = text.replace(/[\s﻿]+$/, '');
    if (!body) return text;
    const last = body.split(/[\s﻿]+/).pop();
    const marked = /[\p{L}\p{N}]/u.test(last)
      ? /[.!?…,;:]/.test(last.replace(CLOSING, '').slice(-1))
      : /[.!?…]/.test(last) || last === ',' || last === ';' || last === ':';
    return marked ? text : body + '.';
  }

  /** Halves of a piece too long for one call: at a clause mark if it has one, else mid-words. */
  function shorter(piece) {
    const clauses = piece.split(CLAUSE_END);
    if (clauses.length > 1) return clauses;
    const words = piece.split(' ');
    if (words.length < 2) return [];
    const half = Math.ceil(words.length / 2);
    return [words.slice(0, half).join(' '), words.slice(half).join(' ')];
  }

  /** Scale samples down, in place, if they peak above PEAK. */
  function limitPeak(samples) {
    let loudest = 0;
    for (let i = 0; i < samples.length; i++) loudest = Math.max(loudest, Math.abs(samples[i]));
    if (loudest > PEAK) for (let i = 0; i < samples.length; i++) samples[i] *= PEAK / loudest;
    return samples;
  }

  const api = { sentences, finished, shorter, limitPeak, PEAK };
  if (typeof module === 'object' && module.exports) module.exports = api; else root.TinyVocaText = api;
})(typeof self !== 'undefined' ? self : globalThis);
