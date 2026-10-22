"""Voxa — server-side text to speech on Kokoro-82M.

Why a server: the browser build of Kokoro is English-only (its bundled phonemizer
ships just ["en"]) and stalls on phones. The Python runtime handles every language
the model was trained for.

Memory is the design constraint. Measured on this project:
    import torch                ~230-404 MB
    one KPipeline               ~1,257 MB   (the 82M model + voice cache + G2P)
    a SECOND KPipeline          +651 MB     <-- this OOM-killed a 2 GB machine
    a second pipeline sharing
      the first one's KModel    +0 MB       <-- so we do exactly that

So: load the weights ONCE, then hand the same KModel to a per-language KPipeline.
Each extra language then costs only its phonemizer.

Japanese additionally needs `pyopenjtalk`, which is not installed by default; it is
reported as unsupported rather than silently failing.
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
MAX_CACHE = 400
MAX_TEXT = 4000

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

# Languages whose G2P needs an extra package; reported honestly if unavailable.
LANG_REQUIRES = {
    'j': 'pyopenjtalk',
}

# The full Kokoro v1.0 voice catalogue (54 voices), taken from the HF repo's voices/ listing.
# Hardcoded because KPipeline.voices only reports the voices already WARM in this process —
# reporting from it gave users a single choice ("1 voices for English"), which was wrong.
VOICES = {
    'a': ['af_heart', 'af_bella', 'af_nicole', 'af_aoede', 'af_kore', 'af_sarah',
          'af_alloy', 'af_nova', 'af_sky', 'af_jessica', 'af_river',
          'am_fenrir', 'am_michael', 'am_puck', 'am_adam', 'am_echo', 'am_eric',
          'am_liam', 'am_onyx', 'am_santa'],
    'b': ['bf_emma', 'bf_isabella', 'bf_alice', 'bf_lily',
          'bm_fable', 'bm_george', 'bm_lewis', 'bm_daniel'],
    'e': ['ef_dora', 'em_alex', 'em_santa'],
    'f': ['ff_siwis'],
    'h': ['hf_alpha', 'hf_beta', 'hm_omega', 'hm_psi'],
    'i': ['if_sara', 'im_nicola'],
    'j': ['jf_alpha', 'jf_gongitsune', 'jf_tebukuro', 'jf_nezumi', 'jm_kumo'],
    'p': ['pf_dora', 'pm_alex', 'pm_santa'],
    'z': ['zf_xiaobei', 'zf_xiaoni', 'zf_xiaoxiao', 'zf_xiaoyi',
          'zm_yunjian', 'zm_yunxi', 'zm_yunxia', 'zm_yunyang'],
}

_shared_model = None
_pipelines = {}
_cache = {}
_model_lock = threading.Lock()
_pipe_lock = threading.Lock()

MAX_CACHE_ITEMS = MAX_CACHE


def get_shared_model():
    """Load Kokoro's weights exactly once for the whole process."""
    global _shared_model
    with _model_lock:
        if _shared_model is None:
            from kokoro import KModel

            log.info('loading Kokoro KModel (shared across languages)')
            _shared_model = KModel(repo_id='hexgrad/Kokoro-82M').to('cpu').eval()
            log.info('KModel ready')
        return _shared_model


def get_pipeline(lang_code):
    """A per-language pipeline sharing the one model. Costs only its phonemizer."""
    if lang_code not in LANG_LABELS:
        raise ValueError(f'unsupported language code: {lang_code}')

    req = LANG_REQUIRES.get(lang_code)
    if req:
        try:
            __import__(req)
        except ImportError:
            raise RuntimeError(
                f'{LANG_LABELS[lang_code]} needs the "{req}" package, which is not installed '
                f'in this image. Add it to the Dockerfile to enable this language.'
            )

    with _pipe_lock:
        if lang_code not in _pipelines:
            from kokoro import KPipeline

            model = get_shared_model()
            log.info('creating pipeline for %s (shared model)', lang_code)
            _pipelines[lang_code] = KPipeline(
                lang_code=lang_code, repo_id='hexgrad/Kokoro-82M', model=model
            )
            log.info('pipeline %s ready', lang_code)
        return _pipelines[lang_code]


def synth(text, voice, speed, lang_code):
    """Yield one WAV (bytes) per sentence chunk."""
    import numpy as np
    import soundfile as sf

    pipe = get_pipeline(lang_code)
    for _, _, audio in pipe(text, voice=voice, speed=speed):
        arr = audio.detach().cpu().numpy() if hasattr(audio, 'detach') else np.asarray(audio)
        buf = io.BytesIO()
        sf.write(buf, arr, 24000, format='WAV', subtype='PCM_16')
        yield buf.getvalue()


def join_wavs(wavs):
    """Concatenate PCM-16 WAVs into one, so multi-sentence text is not truncated."""
    if len(wavs) == 1:
        return wavs[0]
    import wave

    frames, params = [], None
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


def cache_key(text, voice, speed, lang_code):
    h = hashlib.sha256()
    h.update(text.encode('utf-8'))
    h.update(b'|')
    h.update(f'{voice}|{speed}|{lang_code}'.encode())
    return h.hexdigest()


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
.download{display:block;margin-top:12px;text-align:center;background:var(--panel2);color:var(--ink);font-size:14px;font-weight:600;text-decoration:none;border:1px solid var(--line);border-radius:10px;padding:11px 16px;cursor:pointer}
.download:hover{border-color:var(--accent)}
.download[hidden]{display:none}
.dlrow{display:flex;gap:8px;margin-top:12px}
.dlrow .download{margin-top:0;flex:1}
.dlrow .ghost{margin-top:0;flex:1}
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
      <button class="chip" data-lang="i">Italiano</button>
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
    <a id="download" class="download" download="voxa.wav" hidden>⬇ Download audio</a>
    <a id="playerlink" class="download" hidden>Open in player</a>
  </section>
</div>
<div class="card note"><strong>About the model.</strong> Kokoro-82M, 82 million parameters, Apache 2.0.
One set of weights serves every language — only the pronunciation rules change — so all nine
languages share the same memory. Repeated phrases are cached and come back instantly.</div>
</div>
<script>
const $=s=>document.querySelector(s);
let LANG={}, VOICES={};
const langSel=$('#lang'), voiceSel=$('#voice');
Promise.all([
  fetch('/api/languages').then(r=>r.json()),
  fetch('/api/voices').then(r=>r.json())
]).then(([l,v])=>{
  LANG=l.languages||{}; VOICES=v.voices||{};
  langSel.innerHTML=Object.entries(LANG).map(([k,lab])=>`<option value="${k}">${lab}</option>`).join('');
  refreshVoices();
}).catch(()=>{ langSel.innerHTML='<option value="a">English (US)</option>'; });

function refreshVoices(){
  const k=langSel.value, list=VOICES[k]||[];
  voiceSel.innerHTML=list.length?list.map(v=>`<option value="${v}">${v}</option>`).join(''):'<option value="">default</option>';
  const n=list.length;
  $('#langnote').textContent = n ? `${n} voice${n===1?'':'s'} for ${LANG[k]}${n===1?' — the model only ships one for this language':''}.` : '';
}
langSel.addEventListener('change',refreshVoices);
$('#speed').addEventListener('input',e=>$('#speedv').textContent=(+e.target.value).toFixed(2)+'×');
function upd(){const t=$('#text').value;$('#count').textContent=t.length+' characters';$('#est').textContent=t.length?`≈ ${Math.ceil(t.length/14)}s of audio`:'';}
$('#text').addEventListener('input',upd);upd();
const SAMPLES={a:'Voxa turns your words into speech. This audio was generated on a server, so it sounds the same on every device.',
f:'Bonjour. Ceci est Voxa, une voix de synthèse qui fonctionne sur un serveur.',
e:'Hola. Esto es Voxa, una voz sintética generada en un servidor.',
i:'Ciao. Questa è Voxa, una voce sintetica generata su un server.'};
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
      body:JSON.stringify({text,lang:langSel.value,voice:voiceSel.value||null,speed:+$('#speed').value})});
    if(!r.ok){const j=await r.json().catch(()=>({error:'HTTP '+r.status}));throw new Error(j.error||('HTTP '+r.status));}
    const blob=await r.blob();
    const url=URL.createObjectURL(blob);
    const p=$('#player'); p.src=url; p.hidden=false; p.play().catch(()=>{});

    // Download from the blob the browser ALREADY has. A second server request can land on a
    // different machine whose in-memory cache does not contain this audio, which 404'd in
    // production. iOS Safari also ignores the HTML `download` attribute on blob URLs, so use
    // the Web Share API there — that is what surfaces "Save to Files".
    const a=$('#download');
    a.href=url; a.download='voxa.wav'; a.hidden=false;
    const pl=$('#playerlink');
    const realUrl = r.headers.get('X-Voxa-File');
    if (realUrl) { pl.href=realUrl; pl.download='voxa.wav'; pl.hidden=false; } else { pl.hidden=true; }

    const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
                  (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    if (isIOS && navigator.canShare) {
      a.textContent = '⬇ Save or share audio';
      a.onclick = async (ev) => {
        try {
          const file = new File([blob], 'voxa.wav', { type: 'audio/wav' });
          if (navigator.canShare({ files: [file] })) {
            ev.preventDefault();
            await navigator.share({ files: [file], title: 'Voxa audio' });
          }
        } catch (e) { /* user cancelled — fall through to the plain link */ }
      };
    }
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
        # Download endpoint: lets iOS Safari fetch the file directly, which is the only way
        # its share sheet offers "Save to Files" for generated audio.
        if p.startswith('/api/audio/'):
            key = p.rsplit('/', 1)[-1]
            with _pipe_lock:
                audio = _cache.get(key)
            if audio is None:
                return self._send(404, {'error': 'audio not found or expired from cache'})
            return self._send(200, audio, 'audio/wav', {
                'Content-Disposition': f'attachment; filename="voxa-{key[:8]}.wav"',
            })
        if p == '/health':
            return self._send(200, {
                'ok': True,
                'model_loaded': _shared_model is not None,
                'pipelines': list(_pipelines),
                'cached': len(_cache),
            })
        if p == '/api/languages':
            return self._send(200, {'languages': LANG_LABELS})
        if p == '/api/voices':
            return self._send(200, {'voices': VOICES, 'languages': LANG_LABELS})
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
            with _pipe_lock:
                hit = _cache.get(key)
            if hit is not None:
                return self._send(200, hit, 'audio/wav', {'X-Voxa-Cache': 'hit'})

            audio = join_wavs(list(synth(text, voice, speed, lang)))
            if not audio:
                return self._send(500, {'error': 'no audio produced'})

            with _pipe_lock:
                if len(_cache) >= MAX_CACHE_ITEMS:
                    _cache.pop(next(iter(_cache)))
                _cache[key] = audio

            # Content-Disposition gives the browser a real filename. iOS Safari ignores the
            # HTML `download` attribute for blob URLs, so this header is what actually makes
            # "Save to Files" behave on a phone.
            fname = f'voxa-{key[:8]}.wav'
            return self._send(200, audio, 'audio/wav', {
                'X-Voxa-Cache': 'miss',
                'X-Voxa-File': f'/api/audio/{key}',
                'Content-Disposition': f'attachment; filename="{fname}"',
            })
        except Exception as e:
            log.exception('tts failed')
            return self._send(500, {'error': f'{type(e).__name__}: {e}'})


def main():
    try:
        get_pipeline('a')  # warm English
    except Exception as e:
        log.warning('warmup failed: %s', e)
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    log.info('Voxa listening on :%s', PORT)
    srv.serve_forever()


if __name__ == '__main__':
    main()
