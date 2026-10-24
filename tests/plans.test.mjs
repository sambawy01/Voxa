// Tests for how the pages read a /api/quota answer. Run: node --test tests/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { studioLeft } from '../web/book.js';

test('Studio accounts count Standard voices against the monthly fair use', () => {
  assert.deepEqual(studioLeft({ tier: 'paid', wallet: 900, standard_left: 50000 }), { left: 50000, when: 'this month', daily: false });
});

test('Lite accounts spend their wallet', () => {
  assert.deepEqual(studioLeft({ tier: 'free', wallet: 9000, standard_left: 9000 }), { left: 9000, when: 'in your wallet', daily: false });
});

test('visitors before sign-in exists keep a daily allowance; no limit shows nothing', () => {
  assert.deepEqual(studioLeft({ tier: 'guest', limit: 1500, remaining: 700 }), { left: 700, when: 'today', daily: true });
  assert.equal(studioLeft({ tier: 'paid', unlimited: true }), null);
  assert.equal(studioLeft(null), null);
});
