import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  charStatus, clampSpeed, speedLabel, friendlyError, MAX_CHARS, tierLabel, formatBytes, groupVoices,
  benchRows, benchFileName,
} from '../ui/logic.js';

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

test('tierLabel', () => {
  assert.equal(tierLabel('phone'), '📱 Phone');
  assert.equal(tierLabel('gpu'), '🖥️ GPU');
  assert.equal(tierLabel('other'), 'other');
});

test('formatBytes', () => {
  assert.equal(formatBytes(512), '512 B');
  assert.equal(formatBytes(148_500_000), '148.5 MB');
  assert.equal(formatBytes(2_100_000_000), '2.1 GB');
  assert.equal(formatBytes(-1), '');
});

test('groupVoices puts women first and sorts names', () => {
  const g = groupVoices([
    { name: 'Miles', gender: 'male', accent: 'American' },
    { name: 'Nora', gender: 'female', accent: 'American' },
    { name: 'Harper', gender: 'female', accent: 'American' },
  ]);
  assert.deepEqual(g.map(([l]) => l), ['American women', 'American men']);
  assert.deepEqual(g[0][1].map((v) => v.name), ['Harper', 'Nora']);
});

test('groupVoices separates families, women first within each', () => {
  const g = groupVoices([
    { name: 'Ben', gender: 'male', accent: 'American', family: 'Voxa Instant' },
    { name: 'Harper', gender: 'female', accent: 'American', family: 'Voxa Classic' },
    { name: 'Luna', gender: 'female', accent: 'American', family: 'Voxa Instant' },
    { name: 'Miles', gender: 'male', accent: 'American', family: 'Voxa Classic' },
  ]);
  assert.deepEqual(g.map(([l]) => l), [
    'Voxa Classic · American women', 'Voxa Classic · American men',
    'Voxa Instant · American women', 'Voxa Instant · American men',
  ]);
});

test('benchRows formats a report', () => {
  const rows = benchRows({ results: [{
    family: 'Voxa Instant', voice: 'Luna', load_ms: 932, first_audio_ms: 967, rtf: 0.178, model_rss_mb: 277,
  }] });
  assert.deepEqual(rows, [{
    model: 'Voxa Instant (Luna)', load: '0.9 s', first: '1.0 s', speed: '5.6× real time', memory: '277 MB',
  }]);
});

test('benchFileName', () => {
  assert.equal(
    benchFileName({ device: { arch: 'aarch64' } }, new Date('2026-11-02T12:00:00Z')),
    'voxa-speed-test-aarch64-2026-11-02.json',
  );
});
