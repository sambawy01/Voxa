"""Voxa's plans: who may use which Studio voices, how much, and how often.

Voxa Lite (free account)
    On-device voices: unlimited, in the browser; the server never sees them.
    Studio voices: only from the wallet (the welcome credit and any HD packs bought).
    Podcast scripts: 1 a week, up to 5 minutes. Audiobooks: 1 new book a day.
Voxa Studio (paid)
    Standard voices (Kokoro): included, under a monthly fair-use limit.
    HD voices: a monthly allowance first, then the wallet.
    Podcast scripts: 3 a day, up to 20 minutes. Audiobooks: no limit.

The wallet holds characters that never expire: the welcome credit (once per account, with
checks against collecting it many times) and HD packs bought through Paddle. Monthly
allowances reset on the first of each month (UTC). Pages show characters as minutes of audio
at CHARS_PER_MINUTE.

Everything is kept in one SQLite file beside the other usage tables.
"""
import datetime
import os
import sqlite3
import threading
import time

CHARS_PER_MINUTE = 900  # about 15 characters per second of speech
HD_MONTHLY_CHARS = int(os.environ.get('VOXA_HD_MONTHLY_CHARS', '300000'))
STANDARD_MONTHLY_CHARS = int(os.environ.get('VOXA_STANDARD_MONTHLY_CHARS', '3000000'))
WELCOME_CHARS = int(os.environ.get('VOXA_WELCOME_CHARS', '10000'))
WELCOME_PER_NETWORK_DAY = int(os.environ.get('VOXA_WELCOME_PER_NETWORK_DAY', '3'))

# HD packs: the Paddle price id for each comes from the environment.
PACKS = {
    'hd100': {'minutes': 100, 'price': '$4.99', 'env': 'PADDLE_PRICE_HD100'},
    'hd300': {'minutes': 300, 'price': '$11.99', 'env': 'PADDLE_PRICE_HD300'},
    'hd600': {'minutes': 600, 'price': '$19.99', 'env': 'PADDLE_PRICE_HD600'},
}

# (how many, per 'day' or 'week', longest episode in minutes); None = no limit.
PODCAST_RULES = {'free': (1, 'week', 5), 'paid': (3, 'day', 20)}
BOOK_RULES = {'free': (1, 'day'), 'paid': None}


def pack_price_ids():
    """{Paddle price id: pack name} for the packs that are set up."""
    return {os.environ.get(p['env'], ''): name for name, p in PACKS.items() if os.environ.get(p['env'])}


def pack_chars(name):
    return PACKS[name]['minutes'] * CHARS_PER_MINUTE


def minutes(chars):
    return round(chars / CHARS_PER_MINUTE, 1)


class WalletEmpty(Exception):
    """Not enough characters for this request; the message says what to do."""

    def __init__(self, status, requested, message):
        super().__init__(message)
        self.status, self.requested = status, requested


class LimitReached(Exception):
    """A podcast or audiobook count is used up for this day or week."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def month_key(now=None):
    return (now or _now()).strftime('%Y-%m')


def period_key(period, now=None):
    now = now or _now()
    if period == 'day':
        return now.date().isoformat()
    year, week, _ = now.isocalendar()
    return f'{year}-W{week:02d}'


def next_reset(period, now=None):
    """When a 'day', 'week' or 'month' count starts again (UTC), as a timestamp."""
    now = now or _now()
    midnight = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'day':
        return midnight.timestamp()
    if period == 'week':
        return (midnight + datetime.timedelta(days=(7 - now.isoweekday()) % 7)).timestamp()
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first.replace(year=first.year + 1, month=1) if first.month == 12
            else first.replace(month=first.month + 1)).timestamp()


class Plans:
    """The wallet, the monthly allowances and the podcast and audiobook counts.

    `who` is the server's Identity: .user (account id or None), .tier ('free' or 'paid'),
    .visitor (cookie id) and .ip. Only signed-in accounts reach these methods.
    """

    def __init__(self, path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript('''
            CREATE TABLE IF NOT EXISTS wallet (user TEXT PRIMARY KEY, chars INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS wallet_credits (
                id TEXT PRIMARY KEY, user TEXT NOT NULL, chars INTEGER NOT NULL, kind TEXT NOT NULL,
                at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS monthly (
                user TEXT, month TEXT, kind TEXT, chars INTEGER NOT NULL, PRIMARY KEY (user, month, kind));
            CREATE TABLE IF NOT EXISTS counts (
                subject TEXT, kind TEXT, period TEXT, n INTEGER NOT NULL, PRIMARY KEY (subject, kind, period));
            CREATE TABLE IF NOT EXISTS welcome (
                user TEXT PRIMARY KEY, visitor TEXT, ip TEXT, granted INTEGER NOT NULL, at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS books (subject TEXT, day TEXT, book TEXT, PRIMARY KEY (subject, day, book));
        ''')
        self._db.commit()

    # --- the wallet ---------------------------------------------------------------------------

    def _balance(self, user):
        row = self._db.execute('SELECT chars FROM wallet WHERE user = ?', (user,)).fetchone()
        return row[0] if row else 0

    def _add_balance(self, user, n):
        self._db.execute('INSERT INTO wallet (user, chars) VALUES (?, ?) '
                         'ON CONFLICT (user) DO UPDATE SET chars = MAX(0, chars + excluded.chars)', (user, n))

    def credit(self, user, chars, credit_id, kind):
        """Add characters to a wallet once per credit id (a Paddle transaction id, say).
        Returns False when that id was already applied."""
        with self._lock:
            try:
                self._db.execute('INSERT INTO wallet_credits (id, user, chars, kind, at) VALUES (?, ?, ?, ?, ?)',
                                 (credit_id, user, chars, kind, time.time()))
            except sqlite3.IntegrityError:
                return False
            self._add_balance(user, chars)
            self._db.commit()
            return True

    def welcome(self, who):
        """Grant the welcome credit the first time an account is seen, unless this device or
        network has already collected too many. Returns True if granted now."""
        if not who.user or WELCOME_CHARS <= 0:
            return False
        with self._lock:
            if self._db.execute('SELECT 1 FROM welcome WHERE user = ?', (who.user,)).fetchone():
                return False
            device_used = who.visitor and self._db.execute(
                'SELECT 1 FROM welcome WHERE visitor = ? AND granted = 1', (who.visitor,)).fetchone()
            network = self._db.execute('SELECT COUNT(*) FROM welcome WHERE ip = ? AND granted = 1 AND at > ?',
                                       (who.ip, time.time() - 86400)).fetchone()[0]
            granted = not device_used and network < WELCOME_PER_NETWORK_DAY
            self._db.execute('INSERT INTO welcome (user, visitor, ip, granted, at) VALUES (?, ?, ?, ?, ?)',
                             (who.user, who.visitor, who.ip, int(granted), time.time()))
            if granted:
                self._db.execute('INSERT OR IGNORE INTO wallet_credits (id, user, chars, kind, at) '
                                 'VALUES (?, ?, ?, ?, ?)', (f'welcome:{who.user}', who.user, WELCOME_CHARS,
                                                             'welcome', time.time()))
                self._add_balance(who.user, WELCOME_CHARS)
            self._db.commit()
            return granted

    # --- monthly allowances -------------------------------------------------------------------

    def _monthly_used(self, user, kind, month):
        row = self._db.execute('SELECT chars FROM monthly WHERE user = ? AND month = ? AND kind = ?',
                               (user, month, kind)).fetchone()
        return row[0] if row else 0

    def _add_monthly(self, user, kind, month, n):
        self._db.execute('INSERT INTO monthly (user, month, kind, chars) VALUES (?, ?, ?, ?) '
                         'ON CONFLICT (user, month, kind) DO UPDATE SET chars = MAX(0, chars + excluded.chars)',
                         (user, month, kind, n))

    def status(self, who):
        """What this account has left, for the pages."""
        self.welcome(who)
        with self._lock:
            return self._status(who, month_key())

    def _status(self, who, month):
        balance = self._balance(who.user)
        out = {'tier': who.tier, 'wallet': balance, 'wallet_minutes': minutes(balance),
               'month_resets_at': int(next_reset('month')), 'chars_per_minute': CHARS_PER_MINUTE}
        if who.tier == 'paid':
            hd_used = self._monthly_used(who.user, 'hd', month)
            std_used = self._monthly_used(who.user, 'standard', month)
            out['hd_monthly'] = {'limit': HD_MONTHLY_CHARS, 'used': hd_used,
                                 'left': max(0, HD_MONTHLY_CHARS - hd_used)}
            out['standard_monthly'] = {'limit': STANDARD_MONTHLY_CHARS, 'used': std_used,
                                       'left': max(0, STANDARD_MONTHLY_CHARS - std_used)}
            out['hd_left'] = out['hd_monthly']['left'] + balance
            out['standard_left'] = out['standard_monthly']['left']
        else:
            out['hd_left'] = out['standard_left'] = balance
        out['hd_minutes_left'] = minutes(out['hd_left'])
        return out

    def charge(self, who, n, voice_class='standard'):
        """Take n characters of a Studio voice of this class ('standard' or 'hd') or raise
        WalletEmpty. Returns a refund callable, used when the generation fails."""
        self.welcome(who)
        month = month_key()
        with self._lock:
            status = self._status(who, month)
            parts = []  # (where, amount) so a refund puts each back
            if who.tier == 'paid' and voice_class == 'standard':
                if n > status['standard_left']:
                    raise WalletEmpty(status, n, 'This month’s fair-use limit for Standard voices is used up. '
                                      'It resets on the 1st; HD voices and on-device voices still work.')
                parts.append(('standard', n))
            else:
                monthly = status['hd_monthly']['left'] if who.tier == 'paid' else 0
                if n > monthly + status['wallet']:
                    have = minutes(monthly + status['wallet'])
                    raise WalletEmpty(status, n, f'This needs about {minutes(n)} minutes of Studio voice and you '
                                      f'have {have}. Get an HD pack, or use an on-device voice for free.')
                from_month = min(n, monthly)
                if from_month:
                    parts.append(('hd', from_month))
                if n - from_month:
                    parts.append(('wallet', n - from_month))
            for where, amount in parts:
                if where == 'wallet':
                    self._add_balance(who.user, -amount)
                else:
                    self._add_monthly(who.user, where, month, amount)
            self._db.commit()

        def refund():
            with self._lock:
                for where, amount in parts:
                    if where == 'wallet':
                        self._add_balance(who.user, amount)
                    else:
                        self._add_monthly(who.user, where, month, -amount)
                self._db.commit()
        return refund

    # --- podcasts and audiobooks --------------------------------------------------------------

    def count(self, subject, kind, period):
        row = self._db.execute('SELECT n FROM counts WHERE subject = ? AND kind = ? AND period = ?',
                               (subject, kind, period)).fetchone()
        return row[0] if row else 0

    def podcast_status(self, subject, tier):
        rule = PODCAST_RULES.get(tier, PODCAST_RULES['free'])
        if not rule:
            return {'unlimited': True}
        n, period, longest = rule
        with self._lock:
            used = self.count(subject, 'podcast', period_key(period))
        return {'limit': n, 'period': period, 'used': used, 'left': max(0, n - used),
                'max_minutes': longest, 'resets_at': int(next_reset(period))}

    def take_podcast(self, subject, tier, minutes_asked):
        """Count one podcast script or raise LimitReached. Returns a refund callable."""
        rule = PODCAST_RULES.get(tier, PODCAST_RULES['free'])
        if not rule:
            return lambda: None
        n, period, longest = rule
        if minutes_asked > longest:
            raise LimitReached(f'Episodes on your plan are up to {longest} minutes.')
        key = period_key(period)
        with self._lock:
            if self.count(subject, 'podcast', key) >= n:
                hours = max(1, round((next_reset(period) - time.time()) / 3600))
                when = f'in about {hours} h' if period == 'day' else f'on Monday (in about {round(hours / 24)} days)'
                raise LimitReached(f'You have made your {n} podcast{"" if n == 1 else "s"} for this {period}. '
                                   f'The next one is available {when}.')
            self._bump(subject, 'podcast', key, 1)
            self._db.commit()

        def refund():
            with self._lock:
                self._bump(subject, 'podcast', key, -1)
                self._db.commit()
        return refund

    def start_book(self, subject, tier, book):
        """Record that this subject started making a book today, or raise LimitReached.
        Starting the same book again the same day is free."""
        rule = BOOK_RULES.get(tier, BOOK_RULES['free'])
        if not rule:
            return {'unlimited': True}
        n, period = rule
        day = period_key(period)
        with self._lock:
            known = self._db.execute('SELECT 1 FROM books WHERE subject = ? AND day = ? AND book = ?',
                                     (subject, day, book)).fetchone()
            used = self._db.execute('SELECT COUNT(*) FROM books WHERE subject = ? AND day = ?',
                                    (subject, day)).fetchone()[0]
            if not known:
                if used >= n:
                    raise LimitReached(f'Voxa Lite makes {n} new book{"" if n == 1 else "s"} a day; you can keep '
                                       'working on the one you started. Voxa Studio has no limit.')
                self._db.execute('INSERT INTO books (subject, day, book) VALUES (?, ?, ?)', (subject, day, book))
                self._db.execute('DELETE FROM books WHERE day < ?', (day,))
                self._db.commit()
                used += 1
        return {'limit': n, 'used': used, 'left': max(0, n - used), 'resets_at': int(next_reset(period))}

    def _bump(self, subject, kind, period, n):
        self._db.execute('INSERT INTO counts (subject, kind, period, n) VALUES (?, ?, ?, ?) '
                         'ON CONFLICT (subject, kind, period) DO UPDATE SET n = MAX(0, n + excluded.n)',
                         (subject, kind, period, n))
