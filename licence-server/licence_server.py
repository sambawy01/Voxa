"""Voxa licence server: activations for copies of the app sold directly (Paddle).

Separate from the website (server.py): its own process, database and signing key. App
Store and Google Play copies never talk to it; the stores handle those purchases.

Decided in docs/NATIVE_APP_VOICES.md:
- one payment buys the whole app; **two activations** per licence;
- activation happens once online, then the app checks a **signed licence file offline**,
  forever;
- moving machines: deactivate in the app, or a self-service reset (rate-limited), or
  support (admin reset).

Endpoints (JSON in, JSON out):
    POST /v1/activate    {key, device_id, device_name}  -> {licence: {payload, sig}}
    POST /v1/deactivate  {key, activation}              -> {ok}
    POST /v1/reset       {key}                          -> {ok, resets_left}
    GET  /health

Admin (command line, on the server):
    python licence_server.py keygen <secret-file>      new Ed25519 signing key
    python licence_server.py create <db> [note]        new licence key (printed once)
    python licence_server.py admin-reset <db> <key>    clear all activations (support)
    python licence_server.py serve <db> <secret-file> [port]

Licence keys are never stored: the database keeps sha256(key). The signed payload names the
licence by a short prefix of that hash, never the key.

The licence file is {"payload": "<JSON string>", "sig": "<base64 Ed25519 over payload>"}:
the app verifies the exact payload bytes, so Python and Rust never have to agree on JSON
formatting.
"""
import base64
import datetime as dt
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SEATS = 2
RESETS_PER_YEAR = 3
PRODUCT = 'voxa-desktop'
KEY_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # no 0/O, 1/I


class LicenceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def new_key() -> str:
    """A licence key like VOXA-7KQ2-M9XP-4HTR-WB3N (80 random bits)."""
    groups = [''.join(secrets.choice(KEY_ALPHABET) for _ in range(4)) for _ in range(4)]
    return 'VOXA-' + '-'.join(groups)


def normalise_key(key: str) -> str:
    return ''.join(c for c in str(key).upper() if c.isalnum())


def key_hash(key: str) -> str:
    return hashlib.sha256(normalise_key(key).encode()).hexdigest()


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


class Store:
    """SQLite storage. One connection per call keeps it thread-safe for the HTTP server."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS licences (
                    hash TEXT PRIMARY KEY, created TEXT NOT NULL, note TEXT,
                    revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS activations (
                    id TEXT PRIMARY KEY, licence TEXT NOT NULL, device TEXT NOT NULL,
                    name TEXT, created TEXT NOT NULL, released TEXT);
                CREATE TABLE IF NOT EXISTS resets (
                    licence TEXT NOT NULL, at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS act_licence ON activations(licence);
            ''')

    def _db(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def create(self, note: str = '') -> str:
        key = new_key()
        with self.lock, self._db() as db:
            db.execute('INSERT INTO licences (hash, created, note) VALUES (?, ?, ?)',
                       (key_hash(key), now().isoformat(), note))
        return key

    def _licence(self, db, key: str) -> str:
        h = key_hash(key)
        row = db.execute('SELECT revoked FROM licences WHERE hash = ?', (h,)).fetchone()
        if row is None:
            raise LicenceError(404, 'licence key not recognised')
        if row['revoked']:
            raise LicenceError(403, 'this licence has been revoked; contact support')
        return h

    def activate(self, key: str, device: str, name: str) -> dict:
        if not (16 <= len(device) <= 128) or not all(c in '0123456789abcdef' for c in device):
            raise LicenceError(400, 'invalid device id')
        with self.lock, self._db() as db:
            h = self._licence(db, key)
            active = db.execute(
                'SELECT id, device FROM activations WHERE licence = ? AND released IS NULL',
                (h,)).fetchall()
            mine = next((a for a in active if a['device'] == device), None)
            if mine is not None:
                activation = mine['id']  # re-activating the same device is free
            elif len(active) >= SEATS:
                raise LicenceError(409, f'this licence is already active on {SEATS} devices; '
                                        'deactivate one, or reset your activations')
            else:
                activation = str(uuid.uuid4())
                db.execute('INSERT INTO activations (id, licence, device, name, created) '
                           'VALUES (?, ?, ?, ?, ?)',
                           (activation, h, device, str(name)[:80], now().isoformat()))
        return {'v': 1, 'product': PRODUCT, 'licence': h[:16], 'device': device,
                'activation': activation, 'seats': SEATS, 'issued': now().isoformat()}

    def deactivate(self, key: str, activation: str) -> None:
        with self.lock, self._db() as db:
            h = self._licence(db, key)
            cur = db.execute('UPDATE activations SET released = ? '
                             'WHERE id = ? AND licence = ? AND released IS NULL',
                             (now().isoformat(), activation, h))
            if cur.rowcount == 0:
                raise LicenceError(404, 'no such active activation for this licence')

    def reset(self, key: str, admin: bool = False) -> int:
        """Releases every activation. Self-service is limited per rolling year."""
        with self.lock, self._db() as db:
            h = self._licence(db, key)
            year_ago = (now() - dt.timedelta(days=365)).isoformat()
            used = db.execute('SELECT COUNT(*) FROM resets WHERE licence = ? AND at > ?',
                              (h, year_ago)).fetchone()[0]
            if not admin and used >= RESETS_PER_YEAR:
                raise LicenceError(429, 'self-service resets are used up for this year; '
                                        'contact support and we will reset it for you')
            db.execute('UPDATE activations SET released = ? WHERE licence = ? '
                       'AND released IS NULL', (now().isoformat(), h))
            if not admin:
                db.execute('INSERT INTO resets (licence, at) VALUES (?, ?)',
                           (h, now().isoformat()))
                used += 1
        return RESETS_PER_YEAR - used

    def active_count(self, key: str) -> int:
        with self._db() as db:
            return db.execute('SELECT COUNT(*) FROM activations WHERE licence = ? '
                              'AND released IS NULL', (key_hash(key),)).fetchone()[0]


def sign(secret: Ed25519PrivateKey, payload: dict) -> dict:
    text = json.dumps(payload, separators=(',', ':'), sort_keys=True)
    return {'payload': text,
            'sig': base64.b64encode(secret.sign(text.encode())).decode()}


def load_secret(path: str) -> Ed25519PrivateKey:
    raw = bytes.fromhex(open(path).read().strip())
    return Ed25519PrivateKey.from_private_bytes(raw)


def make_handler(store: Store, secret: Ed25519PrivateKey):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'voxa-licence/1'

        def log_message(self, *a):  # no access logs with keys in them
            pass

        def _send(self, status: int, body: dict):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == '/health':
                return self._send(200, {'ok': True})
            self._send(404, {'error': 'not found'})

        def do_POST(self):
            try:
                n = int(self.headers.get('Content-Length') or 0)
                if n > 4096:
                    raise LicenceError(413, 'request too large')
                body = json.loads(self.rfile.read(n) or b'{}')
                if not isinstance(body, dict):
                    raise LicenceError(400, 'expected a JSON object')
                key = str(body.get('key', ''))
                if self.path == '/v1/activate':
                    payload = store.activate(key, str(body.get('device_id', '')),
                                             str(body.get('device_name', '')))
                    return self._send(200, {'licence': sign(secret, payload)})
                if self.path == '/v1/deactivate':
                    store.deactivate(key, str(body.get('activation', '')))
                    return self._send(200, {'ok': True})
                if self.path == '/v1/reset':
                    return self._send(200, {'ok': True, 'resets_left': store.reset(key)})
                raise LicenceError(404, 'not found')
            except LicenceError as e:
                self._send(e.status, {'error': str(e)})
            except (ValueError, json.JSONDecodeError):
                self._send(400, {'error': 'invalid JSON'})

    return Handler


def main(argv: list[str]) -> None:
    cmd = argv[1] if len(argv) > 1 else ''
    if cmd == 'keygen' and len(argv) == 3:
        if os.path.exists(argv[2]):
            sys.exit(f'{argv[2]} exists; refusing to overwrite a key')
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes_raw()
        with open(os.open(argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
            f.write(raw.hex())
        print(key.public_key().public_bytes_raw().hex())
        print('secret key written; the line above is the public key for the app build '
              '(VOXA_LICENCE_PUBKEY)', file=sys.stderr)
    elif cmd == 'create' and len(argv) in (3, 4):
        print(Store(argv[2]).create(argv[3] if len(argv) == 4 else ''))
    elif cmd == 'admin-reset' and len(argv) == 4:
        Store(argv[2]).reset(argv[3], admin=True)
        print('activations released')
    elif cmd == 'serve' and len(argv) in (4, 5):
        port = int(argv[4]) if len(argv) == 5 else 8090
        httpd = ThreadingHTTPServer(('0.0.0.0', port),
                                    make_handler(Store(argv[2]), load_secret(argv[3])))
        print(f'licence server on :{port}')
        httpd.serve_forever()
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main(sys.argv)
