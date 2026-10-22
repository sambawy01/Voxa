"""Voxa's GPU speech service: Kokoro on a GPU for paid users' long jobs (audiobooks).

The same code runs in two places:
  - on Modal, behind gpu/modal_app.py (production);
  - locally, for development and tests:  python gpu/service.py   (PORT, default 8090)

It speaks one endpoint, POST /synthesize, taking the same JSON as Voxa's /api/tts
({text, lang, voice, speed}) and returning a WAV. Requests must carry
"Authorization: Bearer <VOXA_GPU_TOKEN>": the endpoint is public on the internet and every
call costs GPU time, so only the Voxa server, which holds the token, may use it.

Validation, voices and synthesis are Voxa's own (server.py), so a GPU job produces exactly
what the CPU server would, just faster.
"""
import hmac
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import server  # noqa: E402

log = logging.getLogger('voxa.gpu')
MAX_BODY = server.MAX_BODY


class Unauthorized(Exception):
    pass


def check_token(header, token):
    """Constant-time check of an "Authorization: Bearer ..." header against the token."""
    if not token:
        raise Unauthorized('the service has no token configured')
    given = header[7:].strip() if (header or '').startswith('Bearer ') else ''
    if not hmac.compare_digest(given.encode(), token.encode()):
        raise Unauthorized('bad token')


def synthesize(body):
    """WAV bytes for a request body, or BadRequest. Runs on whatever DEVICE server.py uses."""
    text, lang, voice, speed = server.parse_request(body)
    audio = server.join_wavs(list(server.synth(text, voice, speed, lang)))
    if not audio:
        raise RuntimeError('no audio produced')
    return audio


class Handler(BaseHTTPRequestHandler):
    """The local form of the service; Modal wraps synthesize() the same way."""
    protocol_version = 'HTTP/1.1'
    token = os.environ.get('VOXA_GPU_TOKEN', '')

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json'):
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/health':
            return self._send(200, {'ok': True, 'device': server.DEVICE})
        return self._send(404, {'error': 'not found'})

    def do_POST(self):
        if self.path != '/synthesize':
            return self._send(404, {'error': 'not found'})
        try:
            check_token(self.headers.get('Authorization'), self.token)
            n = int(self.headers.get('Content-Length') or 0)
            if n > MAX_BODY:
                self.close_connection = True
                raise server.BadRequest('request too large')
            body = json.loads(self.rfile.read(n) or b'{}')
            return self._send(200, synthesize(body), 'audio/wav')
        except Unauthorized:
            self.close_connection = True
            return self._send(401, {'error': 'unauthorized'})
        except (server.BadRequest, ValueError) as e:
            return self._send(400, {'error': str(e)})
        except Exception:
            log.exception('synthesis failed')
            return self._send(500, {'error': 'synthesis failed'})


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if not Handler.token:
        sys.exit('set VOXA_GPU_TOKEN')
    port = int(os.environ.get('PORT', '8090'))
    server.get_pipeline('a')  # load the model before taking requests
    srv = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    srv.daemon_threads = True
    log.info('Voxa GPU service on :%d (device %s)', port, server.DEVICE)
    srv.serve_forever()


if __name__ == '__main__':
    main()
