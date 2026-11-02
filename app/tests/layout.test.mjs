// Mobile layout (roadmap M10a): renders the real UI in Chromium at phone sizes with a fake
// Tauri backend, and checks it fits: no sideways scrolling, touch-sized controls, inputs
// large enough that iOS doesn't zoom, and the desktop-only "Import voice pack" hidden.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const UI = fileURLToPath(new URL('../ui/', import.meta.url));
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json' };
let server, base, browser;

before(async () => {
  server = http.createServer(async (req, res) => {
    const path = normalize(decodeURIComponent(new URL(req.url, 'http://x').pathname)).replace(/^\/+/, '') || 'index.html';
    try {
      const body = await readFile(join(UI, path));
      res.writeHead(200, { 'content-type': TYPES[extname(path)] ?? 'application/octet-stream' });
      res.end(body);
    } catch {
      res.writeHead(404).end();
    }
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  base = `http://127.0.0.1:${server.address().port}/`;
  browser = await chromium.launch();
});

after(async () => {
  await browser?.close();
  server?.close();
});

/** A fake Tauri backend with a realistic install: voices, models, a cloned voice. */
function fakeTauri(platform) {
  const voice = (id, name, gender, family = 'Voxa Classic', extra = {}) => ({
    id, name, gender, family, accent: 'American', language: 'en-US', model: 'kokoro-en-us',
    model_version: '1.0.0', tier: 'phone', credit: 'Kokoro-82M by hexgrad', default: id === 'harper',
    cloned: false, ...extra,
  });
  const data = {
    platform,
    list_voices: [
      voice('harper', 'Harper', 'female'), voice('miles', 'Miles', 'male'),
      voice('clone-0123456789abcdef', 'Samantha Ellington-Whitfield', '', 'Your voices', { cloned: true, accent: '' }),
    ],
    list_models: [{
      id: 'kokoro-en-us-international-long-identifier', family: 'Voxa Classic', version: '1.0.0', engine: 'kokoro',
      tier: 'phone', languages: ['en-US'], cloning: false, size_bytes: 92_000_000, license: 'Apache-2.0',
      credit: 'Kokoro-82M by hexgrad; ONNX export by k2-fsa/sherpa-onnx', restrictions: null, voices: 28,
    }],
    cleanup_models: [{ id: 'clean-studio', version: '1.0.0', family: 'Voxa Clean Studio', kind: 'studio' }],
    cloning_ready: true,
    licence_status: { enabled: true, active: false, seats: 2 },
    catalogue_configured: true,
    refresh_catalogue: [{
      id: 'supertonic-3', family: 'Voxa World', version: '3.0.0', engine: 'supertonic', tier: 'phone',
      languages: ['es', 'fr', 'de', 'it', 'pt', 'hi'], cloning: false, size_bytes: 260_000_000,
      license: 'LicenseRef-OpenRAIL-M', credit: 'Supertonic 3 by Supertone', voices: 60, installed: false,
    }],
  };
  return `window.__TAURI__ = {
    core: { invoke: async (cmd) => { const d = ${JSON.stringify(data)}; if (cmd in d) return d[cmd]; throw new Error('no ' + cmd); } },
    event: { listen: async () => () => {} },
  };`;
}

async function open(viewport, platform) {
  const ctx = await browser.newContext({ viewport, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
  await ctx.addInitScript(fakeTauri(platform));
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto(base);
  await page.waitForFunction(() => document.querySelectorAll('#voice option').length > 0);
  // Show everything a phone user can reach, including folded sections.
  await page.evaluate(() => { for (const d of document.querySelectorAll('details')) d.open = true; });
  // Error messages carry long unbroken paths on phones; they must wrap, not widen the page.
  const path = '/storage/emulated/0/Android/data/app.voxa.desktop/files/models/kokoro-en-us/1.0.0/model.int8.onnx';
  await page.evaluate((p) => {
    for (const id of ['status', 'libStatus', 'cleanStatus', 'cloneStatus', 'wmResult', 'cleanFile']) {
      document.getElementById(id).textContent = `Couldn't read ${p}: permission denied`;
    }
  }, path);
  return { ctx, page, errors };
}

for (const [name, viewport] of [['small phone', { width: 360, height: 740 }], ['phone', { width: 390, height: 844 }]]) {
  test(`fits a ${name} (${viewport.width}px) without sideways scrolling`, async () => {
    const { ctx, page, errors } = await open(viewport, 'android');
    try {
      assert.deepEqual(errors, []);
      const r = await page.evaluate(() => {
        const vw = document.documentElement.clientWidth;
        const wide = [...document.querySelectorAll('body *')]
          .filter((e) => e.offsetParent !== null && !e.closest('.scroll-x'))
          .filter((e) => e.getBoundingClientRect().right > vw + 0.5)
          .map((e) => `${e.tagName.toLowerCase()}${e.id ? '#' + e.id : ''}.${e.className}`);
        return { scroll: document.documentElement.scrollWidth, vw, wide: wide.slice(0, 5) };
      });
      assert.equal(r.scroll, r.vw, `page scrolls sideways; too wide: ${r.wide.join(', ')}`);
      assert.deepEqual(r.wide, []);
    } finally {
      await ctx.close();
    }
  });
}

test('controls are touch-sized and inputs do not trigger iOS zoom', async () => {
  const { ctx, page } = await open({ width: 390, height: 844 }, 'ios');
  try {
    const bad = await page.evaluate(() => {
      const out = [];
      for (const e of document.querySelectorAll('button, select, input:not([type=range]), textarea')) {
        if (e.offsetParent === null) continue;
        const h = e.getBoundingClientRect().height;
        const fs = parseFloat(getComputedStyle(e).fontSize);
        const id = e.id || e.textContent.trim().slice(0, 20);
        if (h < 44) out.push(`${id}: ${h.toFixed(0)}px tall`);
        if (e.tagName !== 'BUTTON' && fs < 16) out.push(`${id}: font ${fs}px`);
      }
      return out;
    });
    assert.deepEqual(bad, []);
  } finally {
    await ctx.close();
  }
});

test('phones hide the folder import; desktops keep it', async () => {
  for (const [platform, hidden] of [['android', true], ['ios', true], ['macos', false]]) {
    const { ctx, page } = await open({ width: 390, height: 844 }, platform);
    try {
      await page.waitForFunction(() => document.body.dataset.platform);
      assert.equal(await page.locator('#import').isHidden(), hidden, platform);
    } finally {
      await ctx.close();
    }
  }
});
