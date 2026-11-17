"""API keys for agents and scripts, and the small accounts table they need.

A key belongs to one Voxa account and acts as that account: it draws on the same plan limits
and wallet (plans.py). Keys look like `vx_live_<random>`; only a SHA-256 of the key is stored, so
a database leak does not leak keys, and the key itself is shown once, when it is made. Keys are
random with 240 bits of entropy, so a plain hash is enough (no salt or slow hash is needed).

A key carries no tier of its own. The account's tier (free or paid) is recorded when the
account signs in on the web and when Paddle's webhook changes it, so a key follows a cancelled or
new subscription. Making, listing and revoking keys takes a signed-in session, never a key,
so a leaked key cannot mint more keys.
"""
import collections
import hashlib
import secrets
import sqlite3
import threading
import time

PREFIX = 'vx_live_'
MAX_KEYS = 5           # active keys per account
TOUCH_EVERY = 300      # seconds between "last used" writes for one key


class KeyError_(ValueError):
    """A key request that cannot be done; the message is safe to show."""


def hash_key(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def looks_like_key(text):
    return text.startswith(PREFIX)


class RateLimit:
    """At most `limit` calls per `window` seconds for each account (in memory; a restart resets it)."""

    def __init__(self, limit=120, window=60, clock=time.time):
        self.limit, self.window, self.clock = limit, window, clock
        self._hits, self._lock = collections.defaultdict(collections.deque), threading.Lock()

    def hit(self, who):
        """Count a call. Returns 0 if allowed, else the seconds until one would be."""
        now = self.clock()
        with self._lock:
            q = self._hits[who]
            while q and now - q[0] >= self.window:
                q.popleft()
            if len(q) >= self.limit:
                return max(1, int(self.window - (now - q[0])) + 1)
            q.append(now)
            if len(self._hits) > 5000:   # forget accounts that have gone quiet
                for k in [k for k, d in self._hits.items() if not d or now - d[-1] >= self.window]:
                    del self._hits[k]
            return 0


class ApiKeys:
    def __init__(self, path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript('''
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY, user TEXT NOT NULL, name TEXT NOT NULL, hint TEXT NOT NULL,
                hash TEXT NOT NULL UNIQUE, created REAL NOT NULL, last_used REAL, revoked REAL);
            CREATE INDEX IF NOT EXISTS api_keys_user ON api_keys (user);
            CREATE TABLE IF NOT EXISTS accounts (
                user TEXT PRIMARY KEY, tier TEXT NOT NULL, email TEXT, updated REAL NOT NULL);
        ''')
        self._db.commit()
        self._touched = {}

    # --- accounts -----------------------------------------------------------------------------

    def record_account(self, user, tier, email=None):
        """Remember an account's tier ('free' or 'paid'); called when it signs in."""
        tier = 'paid' if tier == 'paid' else 'free'
        with self._lock:
            row = self._db.execute('SELECT tier, email FROM accounts WHERE user = ?', (user,)).fetchone()
            if row == (tier, email):
                return
            self._db.execute('INSERT INTO accounts (user, tier, email, updated) VALUES (?, ?, ?, ?) '
                             'ON CONFLICT (user) DO UPDATE SET tier = excluded.tier, '
                             'email = COALESCE(excluded.email, email), updated = excluded.updated',
                             (user, tier, email, time.time()))
            self._db.commit()

    def tier(self, user):
        row = self._db.execute('SELECT tier FROM accounts WHERE user = ?', (user,)).fetchone()
        return row[0] if row else 'free'

    def email(self, user):
        row = self._db.execute('SELECT email FROM accounts WHERE user = ?', (user,)).fetchone()
        return row[0] if row else None

    # --- keys ---------------------------------------------------------------------------------

    def create(self, user, name):
        """Make a key. Returns ({id, name, hint, created}, secret); the secret is not kept."""
        name = ' '.join(str(name or '').split())[:60] or 'API key'
        with self._lock:
            active = self._db.execute('SELECT COUNT(*) FROM api_keys WHERE user = ? AND revoked IS NULL',
                                      (user,)).fetchone()[0]
            if active >= MAX_KEYS:
                raise KeyError_(f'An account can have {MAX_KEYS} active API keys. Revoke one first.')
            secret = PREFIX + secrets.token_urlsafe(30)
            record = {'id': secrets.token_hex(6), 'name': name, 'hint': secret[:len(PREFIX) + 4] + '…' + secret[-4:],
                      'created': int(time.time())}
            self._db.execute('INSERT INTO api_keys (id, user, name, hint, hash, created) VALUES (?, ?, ?, ?, ?, ?)',
                             (record['id'], user, name, record['hint'], hash_key(secret), record['created']))
            self._db.commit()
        return record, secret

    def list(self, user):
        rows = self._db.execute('SELECT id, name, hint, created, last_used FROM api_keys '
                                'WHERE user = ? AND revoked IS NULL ORDER BY created', (user,)).fetchall()
        return [{'id': i, 'name': n, 'hint': h, 'created': int(c), 'last_used': int(u) if u else None}
                for i, n, h, c, u in rows]

    def revoke(self, user, key_id):
        with self._lock:
            cur = self._db.execute('UPDATE api_keys SET revoked = ? WHERE id = ? AND user = ? AND revoked IS NULL',
                                   (time.time(), key_id, user))
            self._db.commit()
            return cur.rowcount > 0

    def lookup(self, secret):
        """The account id a key belongs to, or None for an unknown or revoked key."""
        if not isinstance(secret, str) or not looks_like_key(secret) or len(secret) > 200:
            return None
        digest = hash_key(secret)
        row = self._db.execute('SELECT id, user FROM api_keys WHERE hash = ? AND revoked IS NULL', (digest,)).fetchone()
        if not row:
            return None
        key_id, user = row
        now = time.time()
        if now - self._touched.get(key_id, 0) > TOUCH_EVERY:
            self._touched[key_id] = now
            with self._lock:
                self._db.execute('UPDATE api_keys SET last_used = ? WHERE id = ?', (now, key_id))
                self._db.commit()
        return user
