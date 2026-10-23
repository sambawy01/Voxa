// Tests for the podcast page's document reading and script handling. Run: node --test tests/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { deflateRawSync } from 'node:zlib';
import { zipStore, crc32, concatBytes } from '../web/book.js';
import {
  readZip, unescapeXml, drawingText, pptxText, pagesToText, limitText,
  chapterToText, textToLines, planParts, joinTurns, scriptChars,
} from '../web/podcast.js';

const enc = new TextEncoder();
const slideXml = (...paras) => `<p:sld><p:cSld><p:spTree>${paras.map(p =>
  `<a:p>${p.split('|').map(r => `<a:r><a:rPr lang="en-US"/><a:t>${r}</a:t></a:r>`).join('')}</a:p>`).join('')}</p:spTree></p:cSld></p:sld>`;

// A zip whose entries are deflated, as PowerPoint writes them.
function zipDeflate(files) {
  const le = (n, size) => Array.from({ length: size }, (_, i) => (n >>> (8 * i)) & 255);
  const local = [], central = [];
  let offset = 0;
  for (const f of files) {
    const name = enc.encode(f.name), data = new Uint8Array(deflateRawSync(f.data)), crc = crc32(f.data);
    const head = [...le(20, 2), ...le(0, 2), ...le(8, 2), ...le(0, 2), ...le(0x21, 2), ...le(crc, 4), ...le(data.length, 4), ...le(f.data.length, 4), ...le(name.length, 2), ...le(0, 2)];
    local.push(new Uint8Array([...le(0x04034b50, 4), ...head, ...name]), data);
    central.push(new Uint8Array([...le(0x02014b50, 4), ...le(20, 2), ...head, ...le(0, 2), ...le(0, 2), ...le(0, 2), ...le(0, 4), ...le(offset, 4), ...name]));
    offset += 30 + name.length + data.length;
  }
  const dir = concatBytes(central);
  return concatBytes([...local, dir, new Uint8Array([...le(0x06054b50, 4), 0, 0, 0, 0, ...le(files.length, 2), ...le(files.length, 2), ...le(dir.length, 4), ...le(offset, 4), 0, 0])]);
}

function deck(zip) {
  const f = (name, s) => ({ name, data: enc.encode(s) });
  return zip([
    f('ppt/presentation.xml', '<p:presentation><p:sldIdLst><p:sldId id="256" r:id="rId3"/><p:sldId id="257" r:id="rId2"/></p:sldIdLst></p:presentation>'),
    f('ppt/_rels/presentation.xml.rels', '<Relationships><Relationship Id="rId2" Target="slides/slide1.xml"/><Relationship Id="rId3" Target="slides/slide2.xml"/></Relationships>'),
    f('ppt/slides/slide1.xml', slideXml('Market size', 'R&amp;D costs fall|ing fast')),
    f('ppt/slides/slide2.xml', slideXml('Solar Co-op', 'Owned by its members')),
    f('ppt/slides/_rels/slide1.xml.rels', '<Relationships><Relationship Id="rId1" Target="../notesSlides/notesSlide7.xml"/></Relationships>'),
    f('ppt/notesSlides/notesSlide7.xml', slideXml('Stress the 40% drop.', '2')),
  ]);
}

test('reads a .pptx in presentation order with speaker notes, stored or deflated', async () => {
  for (const zip of [zipStore, zipDeflate]) {
    const out = await pptxText(deck(zip));
    assert.equal(out.pages, 2);
    assert.equal(out.text, '[Slide 1]\nSolar Co-op\nOwned by its members\n\n[Slide 2]\nMarket size\nR&D costs falling fast\nSpeaker notes: Stress the 40% drop.');
  }
});

test('zip and XML helpers', async () => {
  const files = readZip(zipStore([{ name: 'a.txt', data: enc.encode('hi') }]));
  assert.equal(new TextDecoder().decode(await files.get('a.txt')()), 'hi');
  assert.throws(() => readZip(new Uint8Array(40)), /not a zip/);
  assert.equal(unescapeXml('&lt;a&gt; &amp; &#233;&#x1F600; &bogus;'), '<a> & é😀 &bogus;');
  assert.deepEqual(drawingText('<a:p><a:t>one</a:t><a:br/><a:t>two</a:t></a:p><a:p><a:t> </a:t></a:p>'), ['one two']);
});

test('pages become marked text; empty pages are skipped; long text is cut at a paragraph', () => {
  assert.equal(pagesToText(['First', '', 'Third'], 'Page'), '[Page 1]\nFirst\n\n[Page 3]\nThird');
  const text = 'a'.repeat(90) + '\n\n' + 'b'.repeat(30);
  assert.deepEqual(limitText(text, 100), { text: 'a'.repeat(90), cut: true });
  assert.deepEqual(limitText('short', 100), { text: 'short', cut: false });
});

const hosts = { A: 'Maya', B: 'Leo' };
const chapter = { title: 'Opening', lines: [{ speaker: 'A', text: 'Welcome!' }, { speaker: 'B', text: 'Thanks: glad to be here.' }] };

test('script text round-trips and tolerates loose editing', () => {
  const text = chapterToText(chapter, hosts);
  assert.equal(text, 'Maya: Welcome!\n\nLeo: Thanks: glad to be here.');
  assert.deepEqual(textToLines(text, hosts), chapter.lines);
  // Single newlines between turns, lower-case names, a paragraph without a name, an empty turn.
  assert.deepEqual(textToLines('maya: Hi there.\nLEO: Hello.\n\nAnd one more thing.\n\nMaya:\n\nNote: a label', hosts), [
    { speaker: 'A', text: 'Hi there.' }, { speaker: 'B', text: 'Hello. And one more thing.' }, { speaker: 'A', text: 'Note: a label' }]);
  assert.deepEqual(textToLines('Leo:\n\nMaya:', hosts), []);
  assert.deepEqual(textToLines('No names at all.', hosts), [{ speaker: 'A', text: 'No names at all.' }]);
});

test('parts are planned per chapter, long turns split; turns are joined with pauses', () => {
  const script = { hosts, chapters: [chapter, { title: 'Two', lines: [{ speaker: 'A', text: 'Sentence one. ' + 'x'.repeat(30) }] }] };
  const plan = planParts(script, 20);
  assert.deepEqual(plan.map(p => [p.chapter, p.index, p.speaker]), [[0, 0, 'A'], [0, 1, 'B'], [0, 2, 'B'], [1, 0, 'A'], [1, 1, 'A'], [1, 2, 'A']]);
  assert.equal(scriptChars(script), 8 + 24 + 44);
  const pcm = joinTurns([new Int16Array(10), new Int16Array(10), new Int16Array(10)], ['A', 'A', 'B'], 100);
  assert.equal(pcm.length, 10 + 15 + 10 + 35 + 10);
});
