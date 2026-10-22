"""Load Voxa in a real Chromium, wait for the model and generate speech.

This is the proof that matters: the page renders, the WASM model loads from the
browser, and audio comes out — not just that a build succeeded.
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

PORT = 9334
URL = 'https://sambawy01.github.io/Voxa/'
CHROME = '/opt/data/.pw-browsers/chromium-1243/chrome-linux64/chrome'
PROFILE = tempfile.mkdtemp(prefix='voxa-chrome-')

env = dict(os.environ)
# Chromium needs the Playwright library bundle (libcups and friends). Never leave this
# in the ambient environment — it breaks Python HTTPS and curl (see blender-headless-no-sudo).
env['LD_LIBRARY_PATH'] = (
    '/opt/data/.browserlibs/root/usr/lib/x86_64-linux-gnu:'
    '/opt/data/.libs/root/usr/lib/x86_64-linux-gnu:'
    '/opt/data/xvfb-libs/usr/lib/x86_64-linux-gnu:'
    '/opt/data/pwlibs/usr/lib/x86_64-linux-gnu:'
    '/opt/data/scratch/pw-libs/root/usr/lib/x86_64-linux-gnu:'
    '/usr/lib/x86_64-linux-gnu'
)

proc = subprocess.Popen(
    [CHROME, '--headless=new', f'--remote-debugging-port={PORT}',
     f'--user-data-dir={PROFILE}', '--no-sandbox', '--disable-dev-shm-usage',
     '--disable-gpu', '--disable-features=DBus',
     '--remote-allow-origins=*',
     '--autoplay-policy=no-user-gesture-required',
     '--window-size=1280,900', 'about:blank'],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
)


def wait_devtools(timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/json/version', timeout=3) as r:
                return json.load(r)
        except Exception:
            time.sleep(0.5)
    return None


def ws_url():
    with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/json', timeout=5) as r:
        tabs = json.load(r)
    for t in tabs:
        if t.get('type') == 'page':
            return t['webSocketDebuggerUrl']
    return None


def cdp(ws, method, params=None, _id=[0]):
    _id[0] += 1
    payload = json.dumps({'id': _id[0], 'method': method, 'params': params or {}})
    ws.send(payload)
    while True:
        msg = json.loads(ws.recv())
        if msg.get('id') == _id[0]:
            return msg


def main():
    ver = wait_devtools()
    if not ver:
        print('DEVTOOLS FAILED')
        return 1
    print('chromium:', ver.get('Browser'))

    # navigate via the HTTP endpoint (avoids CDP session juggling for the first load)
    import websocket  # type: ignore
    ws = websocket.create_connection(ws_url(), timeout=30)
    cdp(ws, 'Page.enable')
    cdp(ws, 'Runtime.enable')
    cdp(ws, 'Page.navigate', {'url': URL})
    time.sleep(3)

    # what did the page actually render?
    r = cdp(ws, 'Runtime.evaluate', {
        'expression': '''JSON.stringify({
            title: document.title,
            h1: document.querySelector('h1')?.textContent,
            tagline: document.querySelector('.tagline')?.textContent?.slice(0,80),
            langOptions: document.querySelectorAll('#lang option').length,
            voiceOptions: document.querySelectorAll('#voice option').length,
            hasTextarea: !!document.querySelector('#text'),
            bodyLen: document.body.innerText.length
        })''',
        'returnByValue': True,
    })
    info = json.loads(r['result']['result']['value'])
    print('PAGE:', json.dumps(info, indent=2))

    # JS errors?
    errs = cdp(ws, 'Runtime.evaluate', {
        'expression': 'window.__errs ? window.__errs.join("|") : "no error hook"',
        'returnByValue': True,
    })
    print('ERRORS:', errs['result']['result'].get('value'))

    # try generating. The model download is ~90 MB on first run, so give it time.
    print('generating (this loads the model in-browser)…')
    gen = cdp(ws, 'Runtime.evaluate', {
        'expression': '''(async () => {
            document.querySelector('#text').value = 'Voxa is working. This speech was generated inside your browser.';
            document.querySelector('#text').dispatchEvent(new Event('input'));
            document.querySelector('#go').click();
            const t0 = Date.now();
            while (Date.now() - t0 < 180000) {
                const st = document.querySelector('#status').textContent;
                if (st.startsWith('Done')) {
                    const a = document.querySelector('#player');
                    return JSON.stringify({ok: true, status: st, srcLen: a.src.length, dur: a.duration});
                }
                if (st.startsWith('Error')) return JSON.stringify({ok: false, status: st});
                await new Promise(r => setTimeout(r, 700));
            }
            return JSON.stringify({ok: false, status: 'timeout: ' + document.querySelector('#status').textContent});
        })()''',
        'awaitPromise': True,
        'returnByValue': True,
    })
    print('GENERATE:', gen['result']['result'].get('value'))

    # screenshot as evidence
    shot = cdp(ws, 'Page.captureScreenshot', {'format': 'png'})
    data = shot.get('result', {}).get('data')
    if data:
        with open('/tmp/voxa_live.png', 'wb') as f:
            f.write(base64.b64decode(data))
        print('screenshot: /tmp/voxa_live.png')

    ws.close()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    finally:
        proc.terminate()
