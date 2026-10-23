// Tests for Voxa Reader's text handling. Run: node --test tests/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { cleanForSpeech, readerPlan, detectLang, incomingText, readerLink, shortDuration, FIRST_PART, PART } from '../web/listen.js';

test('copied emails lose quoted replies and raw links', () => {
  const email = 'Hi Sam,\r\n\r\nThe draft is at https://example.com/doc?id=7 for review.\r\n\r\nOn Mon, 20 Oct 2026 at 09:12, Lee <lee@x.com> wrote:\r\n> old message\r\n> more\r\n\r\nThanks,   Ada';
  assert.equal(cleanForSpeech(email), 'Hi Sam,\n\nThe draft is at link for review.\n\nThanks, Ada');
  assert.equal(cleanForSpeech(null), '');
});

test('the first part is short so audio starts fast; later parts are longer', () => {
  const sentence = 'The ferry left the harbour at dawn and nobody on board said a word about the storm. ';
  const text = sentence.repeat(30) + '\n\nSecond paragraph here.';
  const { paras, parts } = readerPlan(text);
  assert.equal(paras.length, 2);
  assert.ok(parts[0].text.length <= FIRST_PART, parts[0].text.length);
  assert.ok(parts.every(p => p.text.length <= PART));
  assert.ok(parts.slice(1, -1).some(p => p.text.length > FIRST_PART));
  assert.deepEqual([...new Set(parts.map(p => p.para))], [0, 1]);
  // Nothing is lost or repeated: the parts add up to the paragraphs, word for word.
  const words = s => s.split(/\s+/).filter(Boolean);
  assert.deepEqual(words(parts.map(p => p.text).join(' ')), words(paras.join(' ')));
  assert.ok(parts.every(p => !p.text.includes('\n')));
});

test('short texts are one part', () => {
  assert.deepEqual(readerPlan('Hello there.').parts, [{ para: 0, text: 'Hello there.' }]);
  assert.deepEqual(readerPlan('   ').parts, []);
});

test('the language is detected from the script or common words', () => {
  const cases = {
    e: 'Hola Marta, gracias por la reunión de ayer. ¿Podemos vernos el lunes para revisar el plan?',
    f: 'Bonjour, merci pour votre message. Je vous envoie le document avec les détails.',
    i: 'Ciao, grazie per il messaggio. Non sono sicuro che questo sia il piano giusto.',
    p: 'Olá, obrigado pela mensagem. Você pode enviar o relatório? Não está pronto.',
    a: 'Hi Sam, thanks for the update. This is the plan for the launch and the budget.',
    j: 'こんにちは、明日の会議について確認したいです。',
    z: '你好，明天的会议改到下午三点。请确认。',
    h: 'नमस्ते, कल की बैठक के बारे में बात करते हैं।',
    unsupported: 'مرحبا، شكرا على رسالتك. سنلتقي غدا.',
  };
  for (const [lang, text] of Object.entries(cases)) assert.equal(detectLang(text, 'a'), lang, text);
  assert.equal(detectLang('ok', 'f'), 'f');  // too little to tell: keep the reader's language
  assert.equal(detectLang(cases.a, 'b'), 'b');  // English stays British for a British-English reader
});

test('text arrives in the fragment or from the share target, never the other way round', () => {
  const text = 'Read me: 50% off & more #1';
  const link = readerLink('https://voxavocals.com', text);
  assert.ok(link.startsWith('https://voxavocals.com/listen#t='));
  assert.equal(incomingText(link), text);
  assert.equal(incomingText('https://voxavocals.com/listen?title=Hello&text=World&url=https%3A%2F%2Fx.io'), 'Hello\n\nWorld\n\nhttps://x.io');
  assert.equal(incomingText('https://voxavocals.com/listen?source=app'), '');
});

test('time left reads naturally', () => {
  assert.equal(shortDuration(20.4), '20 s');
  assert.equal(shortDuration(0.2), '1 s');
  assert.equal(shortDuration(250), '4 min');
});
