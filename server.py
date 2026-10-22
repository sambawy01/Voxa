"""Voxa — server-side text to speech on Kokoro-82M.

Why a server: the browser build of Kokoro is English-only (its bundled phonemizer
ships just `["en"]`), and it stalls on phones. The Python runtime handles all eight
languages the model was trained on and generates in seconds instead of minutes.

Design:
  * model loads ONCE at boot and stays in memory (that is the whole point of a server)
  * generated audio is cached by a hash of (text, voice, speed) so repeat phrases are
    instant and cost nothing — the single biggest saving on a TTS product
  * /api/tts returns audio bytes directly; no filesystem needed
  * /api/voices and /api/languages let the UI stay honest about what exists
"""
import hashlib
import io
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('voxa')

PORT = int(os.environ.get('PORT', '8080'))

# Kokoro lang codes: a=US English, b=UK English, e=Spanish, f=French,
# h=Hindi, i=Italian, j=Japanese, p=Brazilian Portuguese, z=Mandarin
LANG_LABELS = {
    'a': 'English (US)',
    'b': 'English (UK)',
    'e': 'Spanish',
    'f': 'French',
    'h': 'Hindi',
    'i': 'Italian',
    'j': 'Japanese',
    'p': 'Portuguese (BR)',
    'z': 'Chinese (Mandarin)',
}

_pipelines = {}
_cache = {}
_cache_lock = threading.Lock()
_pipe_lock = threading.Lock()

MAX_CACHE = 400
MAX_TEXT = 4000


def get_pipeline(lang_code):
    """One KPipeline per language, created on first use and kept warm."""
    if lang_code not in LANG_LABELS:
        raise ValueError(f'unsupported language code: {lang_code}')
    with _pipe_lock:
        if lang_code not in _pipelines:
            from kokoro import KPipeline
            log.info('loading Kokoro pipeline for %s', lang_code)
            _pipelines[lang_code] = KPipeline(lang_code=lang_code, repo_id='hexgrad/Kokoro-82M')
            log.info('pipeline %s ready', lang_code)
        return _pipelines[lang_code]


def synth(text, voice, speed, lang_code):
    """Yield (wav_bytes, sample_rate) chunks. Kokoro splits long text itself."""
    import numpy as np
    import soundfile as sf

    pipe = get_pipeline(lang_code)
    for _, _, audio in pipe(text, voice=voice, speed=speed):
        arr = audio.detach().cpu().numpy() if hasattr(audio, 'detach') else np.asarray(audio)
        buf = io.BytesIO()
        sf.write(buf, arr, 24000, format='WAV', subtype='PCM_16')
        yield buf.getvalue()


def cache_key(text, voice, speed, lang_code):
    h = hashlib.sha256()
    h.update(text.encode('utf-8'))
    h.update(b'|')
    h.update(f'{voice}|{speed}|{lang_code}'.encode())
    return h.hexdigest()


def join_wavs(wavs):
    """Concatenate several PCM-16 WAV byte strings into one WAV.

    Kokoro yields one WAV per sentence. Joining them gives the caller a single file
    that plays end to end; taking only the first would truncate multi-sentence text.
    All chunks are 24 kHz mono PCM-16, so the frames append directly — no resampling.
    """
    if len(wavs) == 1:
        return wavs[0]
    import struct
    import wave

    frames = []
    params = None
    for blob in wavs:
        with wave.open(io.BytesIO(blob), 'rb') as w:
            p = w.getparams()
            if params is None:
                params = p
            elif (p.nchannels, p.sampwidth, p.framerate) != (
                params.nchannels, params.sampwidth, params.framerate
            ):
                raise ValueError('chunk format mismatch; cannot join')
            frames.append(w.readframes(w.getnframes()))

    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setnchannels(params.nchannels)
        w.setsampwidth(params.sampwidth)
        w.setframerate(params.framerate)
        for f in frames:
            w.writeframes(f)
    return out.getvalue()


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Voxa — text to speech</title>
<style>
:root{--bg:#0f1115;--panel:#171b22;--panel2:#1e2430;--line:#2b3341;--ink:#e9edf4;--dim:#9aa5b5;--accent:#7aa2ff;--good:#6fe3b4}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1200px 600px at 50% -10%,#1b2333 0%,var(--bg) 60%);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;min-height:100vh}
.wrap{max-width:1020px;margin:0 auto;padding:30px 18px 70px}
.brand{display:flex;align-items:center;gap:10px}
.mark{color:var(--accent);font-size:20px}
h1{font-size:26px;margin:0;letter-spacing:.4px;font-weight:680}
.tagline{color:var(--dim);font-size:14px;margin:8px 0 24px;max-width:62ch}
.grid{display:grid;grid-template-columns:1fr 340px;gap:16px}
@media(max-width:860px){.grid{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}
label{display:block;font-size:11.5px;text-transform:uppercase;letter-spacing:.8px;color:var(--dim);margin:16px 0 7px}
label:first-child{margin-top:0}
textarea{width:100%;min-height:210px;background:var(--panel2);color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:13px;font:inherit;resize:vertical}
select{width:100%;background:var(--panel2);color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:10px 11px;font:inherit}
input[type=range]{width:100%;accent-color:var(--accent)}
.meta{display:flex;justify-content:space-between;color:var(--dim);font-size:12px;margin-top:7px;font-variant-numeric:tabular-nums}
.hint{color:var(--dim);font-size:12px;margin:6px 0 0;min-height:18px}
.val{color:var(--good);font-variant-numeric:tabular-nums;font-size:13px}
button.primary{width:100%;margin-top:18px;background:var(--accent);color:#08101f;border:0;border-radius:10px;padding:13px 18px;font:inherit;font-weight:680;cursor:pointer}
button.primary:disabled{opacity:.45;cursor:default}
button.ghost{width:100%;margin-top:8px;background:transparent;color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:10px;font:inherit;cursor:pointer}
.status{font-size:13px;color:var(--dim);margin:10px 0 0;min-height:18px}
.status.busy{color:var(--accent)}
audio{width:100%;margin-top:12px}
audio[hidden]{display:none}
.download{display:inline-block;margin-top:10px;color:var(--accent);font-size:13px;text-decoration:none;border-bottom:1px dotted var(--accent)}
.download[hidden]{display:none}
.note{margin-top:16px;color:var(--dim);font-size:13px;line-height:1.65}
.note strong{color:var(--ink)}
.chips{display:flex;flex-wrap:wrap;gap:7px;margin-top:12px}
.chip{background:var(--panel2);border:1px solid var(--line);color:var(--dim);border-radius:999px;padding:6px 11px;font-size:12px;cursor:pointer;font-family:inherit}
.chip:hover{border-color:var(--accent);color:var(--ink)}
</style></head>
<body><div class="wrap">
<div class="brand"><span class="mark">◉</span><h1>Voxa</h1></div>
<p class="tagline">Text to speech. Nine languages, real voices. Generated on a server, so it sounds the same on every device.</p>
<div class="grid">
  <section class="card">
    <label for="text">Your script</label>
    <textarea id="text" placeholder="Type or paste what you want spoken…"></textarea>
    <div class="meta"><span id="count">0 characters</span><span id="est"></span></div>
    <div class="chips">
      <button class="chip" data-lang="a">English</button>
      <button class="chip" data-lang="f">Français</button>
      <button class="chip" data-lang="e">Español</button>
      <button class="chip" data-lang="j">日本語</button>
      <button class="chip" data-lang="clear">Clear</button>
    </div>
  </section>
  <section class="card">
    <label for="lang">Language</label>
    <select id="lang"></select>
    <p class="hint" id="langnote"></p>
    <label for="voice">Voice</label>
    <select id="voice"></select>
    <p class="hint" id="voicenote"></p>
    <label for="speed">Pacing <span class="val" id="speedv">1.00×</span></label>
    <input type="range" id="speed" min="0.6" max="1.4" step="0.05" value="1">
    <button id="go" class="primary">Generate speech</button>
    <button id="cancel" class="ghost" hidden>Cancel</button>
    <p class="status" id="status"></p>
    <audio id="player" controls hidden></audio>
    <a id="download" class="download" download="voxa.wav" hidden>Download WAV</a>
  </section>
</div>
<div class="card note"><strong>About the model.</strong> Kokoro-82M, 82 million parameters, Apache 2.0.
Running server-side means all eight languages work properly — the browser build only ever shipped an
English phonemizer. Repeated phrases are cached, so they come back instantly.</div>
</div>
<script>
const $=s=>document.querySelector(s);
let LANG={'a':'English (US)','b':'English (UK)','e':'Spanish','f':'French','h':'Hindi','i':'Italian','j':'Japanese','p':'Portuguese (BR)','z':'Chinese (Mandarin)'};
let VOICES={};
const langSel=$('#lang'), voiceSel=$('#voice');
langSel.innerHTML=Object.entries(LANG).map(([k,v])=>`<option value="${k}">${v}</option>`).join('');
function refreshVoices(){
  const k=langSel.value, list=VOICES[k]||[];
  voiceSel.innerHTML=list.map(v=>`<option value="${v}">${v}</option>`).join('');
  $('#langnote').textContent=list.length?`${list.length} voices for ${LANG[k]}.`:`No voices reported — try generating.`;
  $('#voicenote').textContent='';
}
fetch('/api/voices').then(r=>r.json()).then(d=>{VOICES=d.voices||{};refreshVoices();}).catch(()=>refreshVoices());
langSel.addEventListener('change',refreshVoices);
$('#speed').addEventListener('input',e=>$('#speedv').textContent=(+e.target.value).toFixed(2)+'×');
function upd(){const t=$('#text').value;$('#count').textContent=t.length+' characters';$('#est').textContent=t.length?`≈ ${Math.ceil(t.length/14)}s of audio`:'';}
$('#text').addEventListener('input',upd);upd();
const SAMPLES={a:'Voxa turns your words into speech. This audio was generated on a server, so it sounds the same on every device.',f:'Bonjour. Ceci est Voxa, une voix de synthèse qui fonctionne sur un serveur.',e:'Hola. Esto es Voxa, una voz sintética generada en un servidor.',j:'こんにちは。これはVoxaです。'};
document.querySelectorAll('.chip').forEach(c=>c.addEventListener('click',()=>{
  const k=c.dataset.lang;
  if(k==='clear'){$('#text').value='';}else{$('#text').value=SAMPLES[k]||'';langSel.value=k;refreshVoices();}
  upd();
}));
let ctrl=null;
$('#cancel').addEventListener('click',()=>{if(ctrl)ctrl.abort();});
$('#go').addEventListener('click',async()=>{
  const text=$('#text').value.trim(); if(!text){$('#status').textContent='Type something first.';return;}
  const btn=$('#go'), st=$('#status');
  btn.disabled=true; $('#cancel').hidden=false; $('#player').hidden=true; $('#download').hidden=true;
  ctrl=new AbortController();
  const t0=performance.now();
  st.className='status busy'; st.textContent='Generating…';
  try{
    const r=await fetch('/api/tts',{method:'POST',headers:{'Content-Type':'application/json'},
      signal:ctrl.signal,
      body:JSON.stringify({text,lang:langSel.value,voice:voiceSel.value,speed:+$('#speed').value})});
    if(!r.ok){const j=await r.json().catch(()=>({error:'HTTP '+r.status}));throw new Error(j.error||('HTTP '+r.status));}
    const blob=await r.blob();
    const url=URL.createObjectURL(blob);
    const p=$('#player'); p.src=url; p.hidden=false; p.play().catch(()=>{});
    const a=$('#download'); a.href=url; a.hidden=false;
    const cls=r.headers.get('X-Voxa-Cache');
    st.className='status';
    st.textContent=`Done in ${((performance.now()-t0)/1000).toFixed(1)}s`+(cls==='hit'?' · from cache':'');
  }catch(e){
    st.className='status'; st.textContent=e.name==='AbortError'?'Cancelled.':('Error: '+e.message);
  }finally{btn.disabled=false;$('#cancel').hidden=true;ctrl=null;}
});
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json', extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ('/', '/index.html'):
            return self._send(200, PAGE, 'text/html; charset=utf-8')
        if p == '/health':
            return self._send(200, {'ok': True, 'pipelines': list(_pipelines), 'cached': len(_cache)})
        if p == '/api/languages':
            return self._send(200, {'languages': LANG_LABELS})
        if p == '/api/voices':
            # ask a warm pipeline what voices it has; fall back to the known catalogue
            voices = {}
            try:
                pipe = get_pipeline('a')
                if hasattr(pipe, 'voices'):
                    for name in pipe.voices:
                        code = name[0]
                        voices.setdefault(code, []).append(name)
            except Exception as e:
                log.warning('voices probe failed: %s', e)
            return self._send(200, {'voices': voices, 'languages': LANG_LABELS})
        return self._send(404, {'error': 'not found'})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        if urlparse(self.path).path != '/api/tts':
            return self._send(404, {'error': 'not found'})
        try:
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(n) or b'{}')
        except Exception as e:
            return self._send(400, {'error': f'bad json: {e}'})

        text = (body.get('text') or '').strip()
        if not text:
            return self._send(400, {'error': 'text is empty'})
        if len(text) > MAX_TEXT:
            return self._send(400, {'error': f'text longer than {MAX_TEXT} characters'})

        lang = body.get('lang') or 'a'
        speed = float(body.get('speed') or 1.0)
        voice = body.get('voice') or None

        try:
            key = cache_key(text, voice or '', speed, lang)
            with _cache_lock:
                hit = _cache.get(key)
            if hit is not None:
                return self._send(200, hit, 'audio/wav', {'X-Voxa-Cache': 'hit'})

            chunks = list(synth(text, voice, speed, lang))
            if not chunks:
                return self._send(500, {'error': 'no audio produced'})
            # Kokoro yields one WAV per sentence. Concatenate their PCM into a single
            # WAV so a multi-sentence script comes back as one playable file — returning
            # only the first chunk would silently truncate long text.
            audio = join_wavs(chunks)

            with _cache_lock:
                if len(_cache) >= MAX_CACHE:
                    _cache.pop(next(iter(_cache)))
                _cache[key] = audio
            return self._send(200, audio, 'audio/wav', {'X-Voxa-Cache': 'miss'})
        except Exception as e:
            log.exception('tts failed')
            return self._send(500, {'error': f'{type(e).__name__}: {e}'})


def main():
    # warm the English pipeline so the first real request is fast
    try:
        get_pipeline('a')
    except Exception as e:
        log.warning('warmup failed: %s', e)
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    log.info('Voxa listening on :%s', PORT)
    srv.serve_forever()


if __name__ == '__main__':
    main()
