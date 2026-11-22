"""Voxa's own visitor counter: private, cookie-free, no third party.

Counts page views, unique visitors, where visitors came from, phone or computer, the Fly edge
region that served them, and "audio made" events, per UTC day. A visitor is a hash of their
address and browser with a random salt that exists for one day only, so the same person is
one visitor today, and cannot be linked to yesterday or traced back to an address: addresses
are never stored, and when a day ends its salt and hashes are dropped, keeping only the counts.

Bots, link previews, scripts and headless browsers are counted apart, not as visitors.
Stored as one small JSON file per day in <data dir>/analytics.
"""
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import threading
from urllib.parse import urlparse, parse_qs

BOT = re.compile(r'bot|crawl|spider|slurp|preview|facebookexternalhit|embedly|curl|wget|python|httpx|aiohttp|'
                 r'go-http|java/|okhttp|node-fetch|axios|undici|headless|playwright|puppeteer|lighthouse|'
                 r'monitor|uptime|pingdom|scan|fetch|http-client|^$', re.I)
MOBILE = re.compile(r'mobile|iphone|ipad|android', re.I)
SOURCES = [  # referrer host (suffix) -> name
    (('t.co', 'x.com', 'twitter.com'), 'X'), (('google.',), 'Google'), (('bing.com',), 'Bing'),
    (('duckduckgo.com',), 'DuckDuckGo'), (('facebook.com', 'fb.com', 'fb.me'), 'Facebook'),
    (('instagram.com',), 'Instagram'), (('linkedin.com', 'lnkd.in'), 'LinkedIn'), (('reddit.com',), 'Reddit'),
    (('youtube.com', 'youtu.be'), 'YouTube'), (('github.com',), 'GitHub'), (('claude.ai', 'claude.com'), 'Claude'),
    (('chatgpt.com', 'openai.com'), 'ChatGPT'), (('perplexity.ai',), 'Perplexity'), (('t.me', 'telegram.org'), 'Telegram'),
    (('whatsapp.com', 'wa.me'), 'WhatsApp'), (('news.ycombinator.com',), 'Hacker News'), (('producthunt.com',), 'Product Hunt'),
]
EVENTS = ('speak', 'audiobook', 'podcast', 'player')
MAX_SET = 200_000          # visitor hashes kept for one day; past that, new visitors are still counted as views


def source_of(referer, query, own_hosts):
    q = parse_qs(query or '')
    utm = (q.get('utm_source') or q.get('ref') or [''])[0].strip()[:40]
    if utm:
        return utm[:1].upper() + utm[1:]
    host = (urlparse(referer or '').hostname or '').lower()
    if not host:
        return 'Direct'
    if host in own_hosts or host.endswith('.fly.dev') and host.startswith('voxa'):
        return None                     # moving between Voxa pages: not a new arrival
    for hosts, name in SOURCES:
        if any(host == h or host.endswith('.' + h) or (h.endswith('.') and (host.startswith(h) or '.' + h in host)) for h in hosts):
            return name
    return host[4:] if host.startswith('www.') else host


class Analytics:
    def __init__(self, data_dir, own_hosts=('voxavocals.com', 'www.voxavocals.com')):
        self.dir = os.path.join(data_dir, 'analytics')
        os.makedirs(self.dir, exist_ok=True)
        self.own = set(own_hosts)
        self.lock = threading.Lock()
        self.day, self.data, self.seen, self.page_seen, self.event_seen = None, None, set(), {}, {}

    # -- storage
    def _path(self, day):
        return os.path.join(self.dir, f'{day}.json')

    def _load(self, day):
        try:
            with open(self._path(day), encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _write(self, day, data):
        tmp = self._path(day) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, self._path(day))

    def _close_old_days(self):
        """Drop salts and visitor hashes of past days, keeping only counts."""
        for name in os.listdir(self.dir):
            if not name.endswith('.json') or name[:-5] == self.day:
                continue
            d = self._load(name[:-5])
            if d and ('salt' in d or 'hashes' in d):
                d.pop('salt', None); d.pop('hashes', None); d.pop('page_hashes', None)
                self._write(name[:-5], d)

    def _today(self):
        day = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')
        if day != self.day:
            if self.day and self.data:
                self._flush()
            self.day = day
            d = self._load(day) or {}
            self.data = {'day': day, 'salt': d.get('salt') or secrets.token_hex(16), 'views': d.get('views', {}),
                         'visitors': d.get('visitors', 0), 'page_visitors': d.get('page_visitors', {}),
                         'sources': d.get('sources', {}), 'devices': d.get('devices', {}),
                         'regions': d.get('regions', {}), 'events': d.get('events', {}),
                         'event_visitors': d.get('event_visitors', {}), 'hours': d.get('hours', {}),
                         'bots': d.get('bots', 0)}
            self.seen = set(d.get('hashes', []))
            self.page_seen = {k: set(v) for k, v in d.get('page_hashes', {}).items()}
            self.event_seen = {k: set(v) for k, v in d.get('event_hashes', {}).items()}
            self._close_old_days()
        return self.data

    def _flush(self):
        d = dict(self.data)
        d['hashes'] = sorted(self.seen)
        d['page_hashes'] = {k: sorted(v) for k, v in self.page_seen.items()}
        d['event_hashes'] = {k: sorted(v) for k, v in self.event_seen.items()}
        self._write(self.day, d)

    def _visitor(self, data, ip, ua):
        return hashlib.sha256(f'{data["salt"]}|{ip}|{ua}'.encode()).hexdigest()[:20]

    # -- counting
    def page(self, path, ip, ua, referer, query, region):
        ua = (ua or '').strip()
        with self.lock:
            d = self._today()
            if BOT.search(ua):
                d['bots'] += 1
            else:
                v = self._visitor(d, ip, ua)
                d['views'][path] = d['views'].get(path, 0) + 1
                hour = dt.datetime.now(dt.timezone.utc).strftime('%H')
                d['hours'][hour] = d['hours'].get(hour, 0) + 1
                ps = self.page_seen.setdefault(path, set())
                if v not in ps and len(ps) < MAX_SET:
                    ps.add(v)
                    d['page_visitors'][path] = d['page_visitors'].get(path, 0) + 1
                src = source_of(referer, query, self.own)
                if v not in self.seen:
                    if len(self.seen) < MAX_SET:
                        self.seen.add(v)
                    d['visitors'] += 1
                    kind = 'Phone or tablet' if MOBILE.search(ua) else 'Computer'
                    d['devices'][kind] = d['devices'].get(kind, 0) + 1
                    if region:
                        d['regions'][region] = d['regions'].get(region, 0) + 1
                    src = src or 'Direct'
                elif src == 'Direct':
                    src = None    # a later page with no referrer (typed, bookmarked, reloaded): not a new arrival
                if src:   # a new arrival: the first page today, or coming back from another site
                    d['sources'][src] = d['sources'].get(src, 0) + 1
            self._flush()

    def event(self, name, ip, ua):
        if name not in EVENTS or BOT.search((ua or '').strip()):
            return False
        with self.lock:
            d = self._today()
            d['events'][name] = d['events'].get(name, 0) + 1
            v = self._visitor(d, ip, (ua or '').strip())
            es = self.event_seen.setdefault(name, set())
            if v not in es and len(es) < MAX_SET:
                es.add(v)
                d['event_visitors'][name] = d['event_visitors'].get(name, 0) + 1
            self._flush()
        return True

    # -- reading
    def summary(self, days=30):
        with self.lock:
            self._today()
            self._flush()
            out = []
            for name in sorted(os.listdir(self.dir), reverse=True)[:days + 5]:
                if not name.endswith('.json'):
                    continue
                d = self._load(name[:-5])
                if not d:
                    continue
                for k in ('salt', 'hashes', 'page_hashes', 'event_hashes'):
                    d.pop(k, None)
                out.append(d)
            return {'days': out[:days], 'now': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}
