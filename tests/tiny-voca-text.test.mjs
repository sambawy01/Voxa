// Tests for the text handling of the on-device tiny-voca engine. Run: node --test tests/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
// The file is a classic script (the worker loads it with importScripts): run it as one.
const scope = { self: {} };
vm.runInNewContext(readFileSync(new URL('../web/tiny-voca-text.js', import.meta.url), 'utf8'), scope);
const api = scope.self.TinyVocaText;
// Arrays made inside the VM have another prototype; copy them so deepEqual compares contents.
const plain = f => (...args) => { const r = f(...args); return Array.isArray(r) ? [...r] : r; };
const { finished, limitPeak } = api;
const sentences = plain(api.sentences), shorter = plain(api.shorter);

test('text is split into sentences, but not after abbreviations or initials', () => {
  assert.deepEqual(sentences('Hello there. How are you? Fine!'), ['Hello there.', 'How are you?', 'Fine!']);
  assert.deepEqual(sentences('Dr. Smith met J. K. Rowling. They talked.'), ['Dr. Smith met J. K. Rowling.', 'They talked.']);
  assert.deepEqual(sentences('  One\n\ntwo   three.  '), ['One two three.']);
  assert.deepEqual(sentences('... ?!'), []);
});

test('every piece ends with a mark so its last word does not break off', () => {
  assert.equal(finished('No stop'), 'No stop.');
  assert.equal(finished('Has one.'), 'Has one.');
  assert.equal(finished('Quoted "it ends here."'), 'Quoted "it ends here."');
  assert.equal(finished('Trailing space   '), 'Trailing space.');
  assert.equal(finished('Comma,'), 'Comma,');
});

test('a piece that is too long is cut at a clause, else in half, and a lone word cannot be cut', () => {
  assert.deepEqual(shorter('First part, second part; third.'), ['First part,', 'second part;', 'third.']);
  assert.deepEqual(shorter('one two three four five'), ['one two three', 'four five']);
  assert.deepEqual(shorter('Pneumonoultramicroscopic'), []);
});

test('loud audio is scaled down to 0.99 and quiet audio is left alone', () => {
  const loud = limitPeak(Float32Array.of(0.5, -2, 1));
  assert.ok(Math.abs(Math.max(...loud.map(Math.abs)) - 0.99) < 1e-6);
  assert.deepEqual([...limitPeak(Float32Array.of(0.25, -0.5))], [0.25, -0.5]);
});
