// Tests for the audiobook text pipeline. Run: node --test tests/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  linesFromItems, cleanPages, paragraphsFromLines, joinLine, paragraphsFromText,
  findChapters, splitChunks, formatDuration,
} from '../web/book.js';

const item = (str, x, y, w = str.length * 5, h = 10) => ({ str, transform: [h, 0, 0, h, x, y], width: w, height: h });
const line = (text, y) => ({ text, y, h: 10 });

test('items become ordered lines, joining runs and spacing gaps', () => {
  const lines = linesFromItems([
    item('world', 60, 700), item('Hello', 10, 700, 25), item('Sec', 10, 686, 15), item('ond', 25, 686, 15),
  ]);
  assert.deepEqual(lines.map(l => l.text), ['Hello world', 'Second']);
});

test('running headers, footers and page numbers are removed; body text is kept', () => {
  // Body lines differ in words, as real pages do; headers repeat and page numbers count up.
  const words = ['ferry', 'harbour', 'lighthouse', 'channel', 'quay'];
  const pages = words.map((w, n) => [
    line('THE LIGHTHOUSE KEEPER', 800), line(`The ${w} came into view.`, 700),
    line(`Nobody spoke of the ${w} again.`, 686), line(`${n + 1}`, 40),
  ]);
  pages[2].splice(1, 0, line('A line unique to page three.', 750));
  const cleaned = cleanPages(pages);
  assert.ok(cleaned.every(p => !p.some(l => l.text === 'THE LIGHTHOUSE KEEPER' || /^\d$/.test(l.text))));
  assert.ok(cleaned[2].some(l => l.text === 'A line unique to page three.'));
  assert.equal(cleaned[0].length, 2);
});

test('short documents keep their first lines', () => {
  const pages = [[line('Title', 800), line('Body.', 700)], [line('Title', 800), line('More.', 700)]];
  assert.equal(cleanPages(pages)[0][0].text, 'Title');
});

test('paragraphs split on wider gaps and sentence-ending short lines; hyphens mend', () => {
  const lines = [
    line('The ferry left the harbour at a quarter past seven and by the time', 700),
    line('the town had shrunk most passengers had gone be-', 686),
    line('low to escape the wind.', 672),
    line('Mara stayed on deck, as she always had on this long crossing home', 640),
    line('every winter.', 626),
  ];
  const paras = paragraphsFromLines(lines);
  assert.equal(paras.length, 2);
  assert.match(paras[0], /gone below to escape/);
  assert.equal(paras[1], 'Mara stayed on deck, as she always had on this long crossing home every winter.');
});

test('joinLine only mends real hyphenation', () => {
  assert.equal(joinLine('a well-', 'known'), 'a wellknown');
  assert.equal(joinLine('pages 10-', '12'), 'pages 10- 12');
  assert.equal(joinLine('the Anglo-', 'Saxon'), 'the Anglo- Saxon');
  assert.equal(joinLine('soft­', 'ware'), 'soft­ ware');
  assert.equal(joinLine('', 'start'), 'start');
});

test('plain text: blank lines are paragraphs, single newlines are joined', () => {
  assert.deepEqual(paragraphsFromText('One line\nwraps here.\r\n\r\nSecond para-\ngraph.\n\n\n'),
    ['One line wraps here.', 'Second paragraph.']);
});

test('chapters from the PDF outline; front matter excluded by default', () => {
  const paras = [
    { text: 'Copyright 2026.', page: 1 }, { text: 'It begins.', page: 2 },
    { text: 'More of one.', page: 3 }, { text: 'Two starts.', page: 4 },
  ];
  const chapters = findChapters(paras, [{ title: 'Two', page: 4 }, { title: 'One', page: 2 }]);
  assert.deepEqual(chapters.map(c => [c.title, c.include, c.text]), [
    ['Opening pages', false, 'Copyright 2026.'],
    ['One', true, 'It begins.\n\nMore of one.'],
    ['Two', true, 'Two starts.'],
  ]);
});

test('chapters from headings when there is no outline', () => {
  const paras = ['Dedication', 'Chapter 1: The Ferry', 'Text one.', 'CHAPTER TWO', 'Text two.']
    .map((text, i) => ({ text, page: i + 1 }));
  const chapters = findChapters(paras);
  assert.deepEqual(chapters.map(c => c.title), ['Opening pages', 'Chapter 1: The Ferry', 'CHAPTER TWO']);
  assert.equal(chapters[2].text, 'CHAPTER TWO\n\nText two.');  // announced when read aloud
});

test('no outline and no headings: the whole text is one included chapter', () => {
  const chapters = findChapters([{ text: 'Just prose.', page: 1 }, { text: 'More prose.', page: 1 }]);
  assert.deepEqual(chapters.map(c => [c.title, c.include]), [['Full text', true]]);
});

test('chunks respect the limit, prefer paragraph boundaries and lose nothing', () => {
  const para = n => `Sentence ${n} is here.`.repeat(4);
  const text = [para(1), para(2), 'x'.repeat(10) + ' ' + 'A very long sentence without stops '.repeat(20)].join('\n\n');
  const chunks = splitChunks(text, 200);
  assert.ok(chunks.every(c => c.length <= 200), chunks.map(c => c.length).join(','));
  assert.equal(chunks[0], para(1) + '\n\n' + para(2));
  const words = s => s.split(/\s+/).filter(Boolean).join(' ');
  assert.equal(words(chunks.join(' ')), words(text));
});

test('durations read naturally', () => {
  assert.equal(formatDuration(20), '1 min');
  assert.equal(formatDuration(45 * 60), '45 min');
  assert.equal(formatDuration(10 * 3600 + 5 * 60), '10 h 05 min');
});

import { parseWav, encodeWav, floatToInt16, joinPcm, planChunks, bookKey } from '../web/book.js';

test('WAV round trip, and extra chunks before the data are skipped', () => {
  const pcm = Int16Array.from([0, 1000, -1000, 32767, -32768]);
  const wav = encodeWav(pcm, 24000);
  assert.deepEqual(Array.from(parseWav(wav.buffer).pcm), Array.from(pcm));
  // Insert a LIST chunk between fmt and data, as some writers do.
  const list = new Uint8Array([...'LIST'].map(c => c.charCodeAt(0)).concat([4, 0, 0, 0, 1, 2, 3, 4]));
  const withList = new Uint8Array(wav.length + list.length);
  withList.set(wav.subarray(0, 36)); withList.set(list, 36); withList.set(wav.subarray(36), 36 + list.length);
  const parsed = parseWav(withList.buffer);
  assert.equal(parsed.rate, 24000);
  assert.deepEqual(Array.from(parsed.pcm), Array.from(pcm));
  assert.throws(() => parseWav(new Uint8Array(8).buffer), /not a WAV/);
});

test('float samples clamp into 16 bits', () => {
  assert.deepEqual(Array.from(floatToInt16(Float32Array.from([0, 0.5, -1, 2, -2]))), [0, 16384, -32767, 32767, -32768]);
});

test('chunks join with silence between, not after', () => {
  const out = joinPcm([Int16Array.from([1, 1]), Int16Array.from([2])], 10, 0.3);
  assert.deepEqual(Array.from(out), [1, 1, 0, 0, 0, 2]);
});

test('the plan covers included chapters only, in order', () => {
  const plan = planChunks([
    { include: false, text: 'skip me' },
    { include: true, text: 'a'.repeat(30) + '\n\n' + 'b'.repeat(30) },
    { include: true, text: 'c' },
  ], 40);
  assert.deepEqual(plan.map(p => [p.chapter, p.index, p.text[0]]), [[1, 0, 'a'], [1, 1, 'b'], [2, 0, 'c']]);
});

test('the book key changes with text, voice or included chapters', async () => {
  const ch = [{ include: true, title: 'One', text: 'Hello.' }];
  const base = await bookKey(ch, { voice: 'wren' });
  assert.equal(base, await bookKey(ch, { voice: 'wren' }));
  assert.notEqual(base, await bookKey(ch, { voice: 'calvin' }));
  assert.notEqual(base, await bookKey([{ ...ch[0], text: 'Hello!' }], { voice: 'wren' }));
  assert.equal(base, await bookKey([...ch, { include: false, title: 'x', text: 'y' }], { voice: 'wren' }));
});
