import { test } from 'node:test';
import assert from 'node:assert/strict';
import { charStatus, clampSpeed, speedLabel, friendlyError, MAX_CHARS } from '../ui/logic.js';

test('charStatus counts trimmed characters, not UTF-16 units', () => {
  assert.deepEqual(charStatus('  hi  ').count, 2);
  assert.equal(charStatus('😀😀').count, 2);
  assert.equal(charStatus('').ok, false);
  assert.equal(charStatus('a'.repeat(MAX_CHARS)).ok, true);
  const over = charStatus('a'.repeat(MAX_CHARS + 1));
  assert.equal(over.ok, false);
  assert.equal(over.over, true);
  assert.equal(charStatus('abc').label, '3 / 4,000');
});

test('clampSpeed keeps the engine range and 0.05 steps', () => {
  assert.equal(clampSpeed(0.1), 0.5);
  assert.equal(clampSpeed(9), 2);
  assert.equal(clampSpeed('1.23'), 1.25);
  assert.equal(clampSpeed('abc'), 1);
});

test('speedLabel', () => {
  assert.equal(speedLabel(1), '1.0×');
  assert.equal(speedLabel(1.25), '1.25×');
});

test('friendlyError maps engine errors', () => {
  assert.match(friendlyError('text is empty'), /Type something/);
  assert.match(friendlyError('text is 5000 characters; the limit is 4000'), /too long/);
  assert.match(friendlyError(new Error('unknown voice "x"')), /not installed/);
  assert.match(friendlyError('boom'), /boom/);
  assert.equal(friendlyError(undefined), 'Something went wrong.');
});
