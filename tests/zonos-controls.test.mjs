// Tests for ZONOS3's Advanced settings (web/zonos-controls.js). Run: node --test tests/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { buildControls, applyPreset, hasSettings, requestFields, cloneProblem, toBase64, CONSENT_TEXT } from '../web/zonos-controls.js';

// The `ui` document of Zyphra's steering bundle, as GET /v2/controls returns it.
const ui = JSON.parse(readFileSync(new URL('./zonos_ui_fixture.json', import.meta.url)));

test('a slider moves the direction its pole maps to', () => {
  assert.deepEqual(buildControls(ui, { sliders: { gender: 1 } }), { 'identity.gender': 1 });
  assert.deepEqual(buildControls(ui, { sliders: { gender: -1.5 } }), { 'identity.gender': -1.5 });
  assert.deepEqual(buildControls(ui, { sliders: { deep: 3 } }), { 'texture.deep': 3 });
});

test('the projection slider uses whisper below zero and loud above', () => {
  assert.deepEqual(buildControls(ui, { sliders: { projection: -1 } }), { 'delivery.whisper': 1 });
  assert.deepEqual(buildControls(ui, { sliders: { projection: 2 } }), { 'delivery.loud': 2 });
});

test('a control that moves two directions scales both (emphasis)', () => {
  assert.deepEqual(buildControls(ui, { sliders: { emphasis: 2 } }), { 'emphasis_nv_nodur.emphasis': 8, 'mood.arousal': 2 });
});

test('values are clamped to the control range and zeros are dropped', () => {
  assert.deepEqual(buildControls(ui, { sliders: { gender: 99 } }), { 'identity.gender': 3 });
  assert.deepEqual(buildControls(ui, { sliders: { gender: 0, age: 'x', nope: 4 } }), {});
});

test('an accent adds its direction at the chosen strength, within its range', () => {
  assert.deepEqual(buildControls(ui, { sliders: {}, accent: { id: 'british', strength: 1.5 } }), { 'accents_en.british': 1.5 });
  assert.deepEqual(buildControls(ui, { sliders: {}, accent: { id: 'british', strength: 9 } }), { 'accents_en.british': 2 });
  assert.deepEqual(buildControls(ui, { sliders: {}, accent: { id: 'no-such', strength: 1 } }), {});
});

test('every preset resolves to known controls and accents', () => {
  const ids = new Set(ui.controls.map(c => c.id)), accents = new Set(ui.accents.map(a => a.id));
  for (const name of Object.keys(ui.presets)) {
    const s = applyPreset(ui, name);
    for (const id of Object.keys(s.sliders)) assert.ok(ids.has(id), `${name}: ${id}`);
    if (s.accent) assert.ok(accents.has(s.accent.id), `${name}: ${s.accent.id}`);
    assert.ok(Object.keys(buildControls(ui, s)).length > 0, name);
  }
  assert.equal(applyPreset(ui, 'nothing'), null);
});

test('only directions the bundle actually has are ever sent', () => {
  const known = new Set(JSON.parse(readFileSync(new URL('./zonos_directions.json', import.meta.url))));
  for (const c of ui.controls) for (const m of Object.values(c.map)) for (const d of Object.keys(m)) assert.ok(known.has(d), d);
  for (const a of ui.accents) for (const d of Object.keys(a.map)) assert.ok(known.has(d), d);
});

test('no settings means no extra request fields', () => {
  assert.equal(hasSettings({ sliders: { gender: 0 }, accent: null }), false);
  assert.deepEqual(requestFields(ui, { sliders: {} }), {});
  assert.deepEqual(requestFields(ui, { sliders: { age: 1 } }, { speaker_mode: 'embedding' }),
    { speaker_mode: 'embedding', controls: { 'identity.age': 1 } });
});

test('cloning needs consent and a usable clip', () => {
  const file = { size: 1000 };
  assert.match(cloneProblem({ consent: true, file: null }), /Choose or record/);
  assert.match(cloneProblem({ consent: false, file }), /permission/);
  assert.match(cloneProblem({ consent: true, file: { size: 99e6 } }), /too large/);
  assert.match(cloneProblem({ consent: true, file, seconds: 1 }), /too short/);
  assert.equal(cloneProblem({ consent: true, file, seconds: 12 }), '');
  assert.match(CONSENT_TEXT, /permission/);
  assert.match(CONSENT_TEXT, /AI-generated/);
});

test('base64 matches Buffer for large input', () => {
  const bytes = Uint8Array.from({ length: 100000 }, (_, i) => i * 7 % 256);
  assert.equal(toBase64(bytes), Buffer.from(bytes).toString('base64'));
});
