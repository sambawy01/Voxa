// Tests for the ZONOS3 Studio helpers (web/zonos-studio-logic.js). Run: node --test tests/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { diffWords, changedCount, parseScript, makeProject, readProject, toNpy, fromNpy, FloatStream, concat, MAX_LINES } from '../web/zonos-studio-logic.js';

test('the word diff shows swaps, additions and removals, ignoring case and punctuation', () => {
  const ops = diffWords('Meet me at the park at noon.', 'meet me at the beach at 3 pm');
  assert.deepEqual(ops.filter(o => o.type !== 'keep').map(o => [o.type, o.a, o.b]),
    [['swap', 'park', 'beach'], ['add', undefined, '3'], ['swap', 'noon.', 'pm']]);
  assert.equal(changedCount(diffWords('same words here', 'Same words, here!')), 0);
  assert.deepEqual(diffWords('a b c', 'a c').filter(o => o.type === 'del').map(o => o.a), ['b']);
  assert.deepEqual(diffWords('', 'new text').map(o => o.type), ['add', 'add']);
});

test('a script is split by cast name; unmarked lines continue the turn', () => {
  const r = parseScript('June: Hello there.\nCalvin: Hi!\nHow are you?\n\njune: Fine.', ['June', 'Calvin']);
  assert.deepEqual(r.lines, [{ speaker: 'June', text: 'Hello there.' }, { speaker: 'Calvin', text: 'Hi! How are you?' }, { speaker: 'June', text: 'Fine.' }]);
  assert.deepEqual(r.problems, []);
});

test('script problems are readable', () => {
  assert.match(parseScript('Hello there.', ['June']).problems[0], /start each line with a cast name/);
  assert.match(parseScript('Bob: Hello.', ['June']).problems[0], /"Bob" is not in the cast/);
  assert.match(parseScript('June:', ['June']).problems[0], /nothing to say/);
  assert.match(parseScript(Array(MAX_LINES + 1).fill('June: hi').join('\n'), ['June']).problems[0], /limit/);
});

test('projects round trip and reject strangers', () => {
  const p = makeProject('Demo', [{ name: 'June', voice: 'x-zo-a', state: { sliders: { age: 1 } } }], 'June: Hi.');
  const back = readProject(JSON.stringify(p));
  assert.deepEqual(back, p);
  assert.throws(() => readProject('{"a":1}'), /not a Voxa Studio project/);
  assert.throws(() => readProject('nope'), /not a Voxa Studio project/);
  assert.equal(readProject({ ...p, cast: [{ name: '', voice: 'x' }, ...p.cast] }).cast.length, 1);
});

test('npy files match NumPy byte for byte and round trip', () => {
  const values = Float32Array.from({ length: 300 }, (_, i) => Math.sin(i) * 3.5);
  const mine = toNpy(values);
  assert.equal((mine.length - values.length * 4) % 64, 0); // the header is padded to 64 bytes
  assert.deepEqual([...fromNpy(mine)], [...values]);
  let numpy;
  try {
    numpy = execFileSync('python3', ['-I', '-c',
      'import numpy,sys; a=numpy.array([numpy.sin(i)*3.5 for i in range(300)],dtype="<f4"); numpy.save(sys.stdout.buffer,a)'], { maxBuffer: 1 << 20 });
  } catch { return; } // NumPy not installed here
  assert.equal(Buffer.from(mine).equals(numpy), true);
  assert.deepEqual([...fromNpy(numpy)], [...values]);
});

test('npy files that are not a speaker vector are refused', () => {
  assert.throws(() => fromNpy(new Uint8Array([1, 2, 3])), /not a \.npy/);
  const bad = toNpy(new Float32Array(4));
  assert.throws(() => fromNpy(bad), /single speaker vector/);
  const cut = toNpy(new Float32Array(64)).slice(0, 200);
  assert.throws(() => fromNpy(cut), /cut short/);
  const nan = toNpy(Float32Array.from({ length: 16 }, () => NaN));
  assert.throws(() => fromNpy(nan), /invalid numbers/);
});

test('streamed bytes become whole samples whatever the chunk sizes', () => {
  const samples = Float32Array.from({ length: 1000 }, (_, i) => i / 7);
  const bytes = new Uint8Array(samples.buffer);
  const s = new FloatStream(), parts = [];
  for (let at = 0; at < bytes.length;) { const n = 1 + (at * 31 % 13); parts.push(s.push(bytes.subarray(at, at + n))); at += n; }
  assert.deepEqual([...concat(parts)], [...samples]);
  assert.equal(s.rest.length, 0);
});
