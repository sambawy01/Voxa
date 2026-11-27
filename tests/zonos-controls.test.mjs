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

import { interp, identityOf, isSameVoice, moodPoint, setMoodFromPad, chooseEmotion, nearbyVoices, blendFields, samplingFields,
  collapseWithMap, chunkText, spansForChunk, toSpan } from '../web/zonos-controls.js';

test('identity falls as controls move and "same voice" follows the threshold', () => {
  assert.equal(identityOf(ui, { sliders: {} }), 1);
  assert.ok(Math.abs(interp([[0, 0.9], [1, 0.7]], 0.5) - 0.8) < 1e-9);
  const small = { sliders: { gender: 0.2 } }, big = { sliders: { gender: 2 } };
  assert.ok(identityOf(ui, big) < identityOf(ui, small));
  assert.equal(isSameVoice(ui, big), false);
  assert.ok(identityOf(ui, { sliders: {}, accent: { id: 'british', strength: 2 } }) < 0.85);
});

test('the mood pad sets valence and arousal and clears single emotions', () => {
  const s = setMoodFromPad(ui, { sliders: { happy: 4, gender: 1 } }, 1, -0.5);
  assert.equal(s.sliders.happy, undefined);
  assert.equal(s.sliders.gender, 1);
  assert.equal(s.sliders.valence, 4);
  assert.equal(s.sliders.arousal, -2);
  const p = moodPoint(ui, s);
  assert.ok(p.x > 0 && p.y < 0);
});

test('choosing an emotion uses Zyphra\'s recommended dose', () => {
  const happy = ui.moods.find(m => m.id === 'happy');
  const s = chooseEmotion(ui, { sliders: { valence: 3, sad: 2 } }, 'happy');
  assert.equal(s.sliders[happy.control], happy.dose);
  assert.equal(s.sliders.valence, 0);
  assert.equal(s.sliders.sad, undefined);
  assert.deepEqual(chooseEmotion(ui, { sliders: { a: 1 } }, 'nope'), { sliders: { a: 1 } });
});

test('nearby voices stay inside the safe range and differ from each other', () => {
  let seed = 7; const rng = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const list = nearbyVoices(ui, { sliders: { gender: 1 } }, 5, rng);
  assert.equal(list.length, 5);
  const byId = Object.fromEntries(ui.controls.map(c => [c.id, c]));
  for (const v of list) for (const [id, x] of Object.entries(v.sliders)) assert.ok(x >= byId[id].safe[0] && x <= byId[id].safe[1], `${id}=${x}`);
  assert.equal(new Set(list.map(v => JSON.stringify(v.sliders))).size, 5);
});

test('a blend needs two different voices; sampling sends only changes', () => {
  assert.deepEqual(blendFields('a', 'b', 0.3), { speaker_blend_embedding_id_a: 'a', speaker_blend_embedding_id_b: 'b', speaker_blend_t: 0.3 });
  assert.deepEqual(blendFields('a', 'a', 0.3), {});
  assert.deepEqual(blendFields('a', null, 0.3), {});
  assert.deepEqual(samplingFields({ temperature: 0.75, top_p: 0.9, laci: true }), { top_p: 0.9, laci: true });
  assert.deepEqual(samplingFields({}), {});
});

test('spans follow the words they were marked on, through whitespace and chunking', () => {
  const raw = 'Hello   there.\n\nThis is   the second sentence.';
  const { text, map } = collapseWithMap(raw);
  assert.equal(text, 'Hello there. This is the second sentence.');
  const start = raw.indexOf('second'), end = start + 'second'.length;
  const sp = toSpan(ui, { start, end }, map, { happy: 4 });
  assert.equal(text.slice(sp.start, sp.end), 'second');
  assert.ok(sp.controls['emotions.happy'] > 0);
  const chunks = chunkText(text, 15);
  assert.ok(chunks.length >= 2);
  const hit = chunks.map(c => spansForChunk([sp], c)).filter(a => a.length);
  assert.equal(hit.length, 1);
  const c = chunks.find(c => spansForChunk([sp], c).length), [one] = spansForChunk([sp], c);
  assert.equal(c.text.slice(one.start, one.end), 'second');
  assert.equal(toSpan(ui, { start: 3, end: 3 }, map, { happy: 4 }), null);
  assert.equal(toSpan(ui, { start, end }, map, {}), null);
});

import { withEmbeddingMode } from '../web/zonos-controls.js';

test('speaker files and blends ask for embedding mode, which the engine requires', () => {
  assert.equal(withEmbeddingMode({ speaker_embedding_base64: 'x' }).speaker_mode, 'embedding');
  assert.equal(withEmbeddingMode({ speaker_blend_embedding_id_a: 'a', speaker_blend_embedding_id_b: 'b', speaker_blend_t: 0.5 }).speaker_mode, 'embedding');
  assert.deepEqual(withEmbeddingMode({ controls: { a: 1 } }), { controls: { a: 1 } });
  assert.equal(withEmbeddingMode({ speaker_embedding_base64: 'x', speaker_mode: 'reference' }).speaker_mode, 'reference');
});
