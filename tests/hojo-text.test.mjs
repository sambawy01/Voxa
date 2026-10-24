// Tests for the on-device Hojo engine's text handling. Run: node --test tests/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { englishNumber, englishYear, chineseNumber, normalizeForHojo, splitForHojo, HojoTokenizer, promptIds } from '../web/hojo-text.js';

test('English numbers, years, money, times and percentages are spelled out', () => {
  assert.equal(englishNumber(0), 'zero');
  assert.equal(englishNumber(1234567), 'one million two hundred thirty-four thousand five hundred sixty-seven');
  assert.equal(englishYear(1984), 'nineteen eighty-four');
  assert.equal(englishYear(2005), 'two thousand five');
  assert.equal(englishYear(1906), 'nineteen oh six');
  assert.equal(normalizeForHojo('It costs $5.20 and $1 for 1,250 people in 2024.', 'a'),
    'It costs five dollars twenty cents and one dollar for one thousand two hundred fifty people in twenty twenty-four.');
  assert.equal(normalizeForHojo('Up 12.5% to $3 million on the 21st, at 10:30 a.m.', 'a'),
    'Up twelve point five percent to three million dollars on the twenty-first, at ten thirty a m.');
  assert.equal(normalizeForHojo('Room 007 is -4 degrees at 3pm. R&D', 'a'), 'Room zero zero seven is minus four degrees at three p m. R and D');
  assert.equal(normalizeForHojo('Meet at 9 a.m. Then lunch at 12:15 p.m. on Friday.', 'a'), 'Meet at nine a m. Then lunch at twelve fifteen p m on Friday.');
});

test('Chinese numbers, dates, percentages and phone numbers read naturally', () => {
  assert.equal(chineseNumber(10), '十');
  assert.equal(chineseNumber(15), '十五');
  assert.equal(chineseNumber(10005), '一万零五');
  assert.equal(chineseNumber(100000), '十万');
  assert.equal(chineseNumber(1250), '一千二百五十');
  assert.equal(normalizeForHojo('2024年10月24日，气温18度，下午3:30开会。', 'z'), '二零二四年十月二十四日，气温十八度，下午三点三十分开会。');
  assert.equal(normalizeForHojo('增长12.5%，-5℃，电话13800138000', 'z'), '增长百分之十二点五，负五摄氏度，电话一三八零零一三八零零零');
});

test('brackets never reach the model, where they would be control tokens', () => {
  assert.equal(normalizeForHojo('Say [123] <zh_a1> {x}', 'a'), 'Say one hundred twenty-three zh_a1 x');
});

test('long text is split into pieces the model can say in one go', () => {
  const long = 'First sentence here. Second one! ' + 'A long clause, '.repeat(30) + 'the end.';
  const pieces = splitForHojo(long, 'a');
  assert.ok(pieces.length > 1);
  assert.ok(pieces.every(p => p.length <= 220), pieces.map(p => p.length).join(','));
  assert.equal(pieces.join(' ').replace(/\s+/g, ' '), long.trim());
  assert.deepEqual(splitForHojo('Hi. Bye.', 'a'), ['Hi. Bye.']);
  const zh = '第一句话。' + '这是一个很长的句子，'.repeat(20) + '结束。';
  assert.ok(splitForHojo(zh, 'z').every(p => p.length <= 80));
  assert.deepEqual(splitForHojo('... !!', 'a'), []);
});

// A tiny tokenizer in the shape of hojo.json: lowercase, digits alone, BPE merges.
const META = {
  unk: 0, vocabulary: { '[UNK]': 0, h: 1, e: 2, l: 3, o: 4, ' ': 5, he: 6, ll: 7, hell: 8, '1': 9, '2': 10, '!': 11 },
  merges: ['h e', 'l l', 'he ll'],
  special: { '[target_text_start]': 100, '[target_text_end]': 101, '[spk_start]': 102, '[spk_end]': 103, '[target_speech_start]': 104 },
  speaker_slots: [200, 201],
};

test('the tokenizer lowercases, splits digits and applies merges in rank order', () => {
  const t = new HojoTokenizer(META);
  assert.deepEqual(t.encode('Hello  12!'), [8, 4, 5, 9, 10, 11]);
  assert.deepEqual(t.encode('héllo'), [1, 0, 7, 4]);  // é is unknown
  assert.deepEqual(promptIds(t, META, 'hello'), [100, 8, 4, 101, 102, 200, 201, 103, 104]);
});
