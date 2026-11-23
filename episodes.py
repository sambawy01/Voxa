"""Voxa's shows: podcast episodes made with Voxa, published on the site under /shows.

Episodes live on the data volume (<data dir>/episodes/<slug>/), not in the code, so the owner
publishes one by uploading it with the owner API key; no deploy is needed:

    PUT /api/episodes/<slug>            episode.json (title, series, number, description, ...)
    PUT /api/episodes/<slug>/<file>     video.mp4, audio.mp3, cover.jpg, share.jpg (raw body)
    DELETE /api/episodes/<slug>

Readers get GET /api/episodes (the list), /episodes/<slug>/<file> (with byte ranges, which
phones need to play and seek), /shows/<slug> (a page with link-preview tags), and an RSS
feed at /shows/feed.xml for podcast apps.
"""
import datetime as dt
import email.utils
import html
import json
import os
import re
import shutil

SLUG = re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')
FILES = {'video.mp4': 'video/mp4', 'audio.mp3': 'audio/mpeg', 'cover.jpg': 'image/jpeg', 'cover.png': 'image/png',
         'share.jpg': 'image/jpeg'}   # share.jpg: the 1200x630 link-preview image (X, Facebook, LinkedIn...)
MAX_FILE = 200 * 1024 * 1024
FIELDS = {'title': str, 'series': str, 'number': int, 'description': str, 'published': str, 'duration': (int, float),
          'hosts': list, 'made_in': str, 'x_url': str, 'transcript': list, 'language': str, 'draft': bool}


class Episodes:
    def __init__(self, data_dir):
        self.dir = os.path.join(data_dir, 'episodes')
        os.makedirs(self.dir, exist_ok=True)

    def _ep_dir(self, slug):
        if not SLUG.match(slug or ''):
            raise ValueError('An episode name is lower-case letters, digits and dashes.')
        return os.path.join(self.dir, slug)

    # -- writing (owner)
    def put_meta(self, slug, meta):
        if not isinstance(meta, dict):
            raise ValueError('Send the episode as a JSON object.')
        clean = {}
        for k, v in meta.items():
            if k in FIELDS and isinstance(v, FIELDS[k]):
                clean[k] = v
        if not clean.get('title'):
            raise ValueError('An episode needs a "title".')
        for line in clean.get('transcript', []):
            if not (isinstance(line, dict) and isinstance(line.get('text'), str)):
                raise ValueError('"transcript" is a list of {speaker, text, start, end}.')
        clean.setdefault('published', dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d'))
        d = self._ep_dir(slug)
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, 'episode.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(clean, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(d, 'episode.json'))
        return self.get(slug)

    def put_file(self, slug, name, stream, length):
        if name not in FILES:
            raise ValueError(f'Files are {", ".join(FILES)}.')
        if not 0 < length <= MAX_FILE:
            raise ValueError(f'A file is 1 byte to {MAX_FILE // 2**20} MB.')
        d = self._ep_dir(slug)
        if not os.path.exists(os.path.join(d, 'episode.json')):
            raise ValueError('Create the episode (its episode.json) first.')
        tmp = os.path.join(d, name + '.part')
        left = length
        with open(tmp, 'wb') as f:
            while left:
                block = stream.read(min(left, 1 << 20))
                if not block:
                    raise ValueError('The upload ended early.')
                f.write(block)
                left -= len(block)
        os.replace(tmp, os.path.join(d, name))
        return {'slug': slug, 'file': name, 'bytes': length}

    def delete(self, slug):
        d = self._ep_dir(slug)
        if not os.path.isdir(d):
            return False
        shutil.rmtree(d)
        return True

    # -- reading
    def get(self, slug, drafts=True):
        d = self._ep_dir(slug)
        try:
            with open(os.path.join(d, 'episode.json'), encoding='utf-8') as f:
                meta = json.load(f)
        except (OSError, ValueError):
            return None
        if meta.get('draft') and not drafts:
            return None
        files = {n: os.path.getsize(os.path.join(d, n)) for n in FILES if os.path.exists(os.path.join(d, n))}
        cover = next((n for n in ('cover.jpg', 'cover.png') if n in files), None)
        return {**meta, 'slug': slug, 'url': f'/shows/{slug}',
                'video': f'/episodes/{slug}/video.mp4' if 'video.mp4' in files else None,
                'audio': f'/episodes/{slug}/audio.mp3' if 'audio.mp3' in files else None,
                'cover': f'/episodes/{slug}/{cover}' if cover else None,
                'share': f'/episodes/{slug}/share.jpg' if 'share.jpg' in files else None,
                'audio_bytes': files.get('audio.mp3')}

    def list(self, drafts=False):
        out = [e for e in (self.get(s, drafts) for s in sorted(os.listdir(self.dir)) if SLUG.match(s)) if e]
        return sorted(out, key=lambda e: (e.get('published', ''), e.get('number', 0)), reverse=True)

    def file(self, slug, name):
        """(path, content type) of a published episode's file, or None."""
        if name not in FILES or not self.get(slug, drafts=False):
            return None
        path = os.path.join(self._ep_dir(slug), name)
        return (path, FILES[name]) if os.path.isfile(path) else None

    # -- the podcast feed
    def rss(self, base):
        esc = lambda s: html.escape(str(s or ''), quote=True)
        items = []
        for e in self.list():
            if not e.get('audio'):
                continue
            when = dt.datetime.strptime(e['published'][:10], '%Y-%m-%d').replace(tzinfo=dt.timezone.utc)
            secs = int(e.get('duration') or 0)
            items.append(f'''  <item>
    <title>{esc(e["title"])}</title>
    <description>{esc(e.get("description"))}</description>
    <link>{base}{e["url"]}</link>
    <guid isPermaLink="false">voxa-{esc(e["slug"])}</guid>
    <pubDate>{email.utils.format_datetime(when)}</pubDate>
    <enclosure url="{base}{e["audio"]}" length="{e.get("audio_bytes") or 0}" type="audio/mpeg"/>
    <itunes:duration>{secs // 60}:{secs % 60:02d}</itunes:duration>
    {f'<itunes:episode>{int(e["number"])}</itunes:episode>' if e.get("number") else ''}
    {f'<itunes:image href="{base}{e["cover"]}"/>' if e.get("cover") else ''}
  </item>''')
        cover = next((e['cover'] for e in self.list() if e.get('cover')), None)
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>Voxa Shows</title>
  <link>{base}/shows</link>
  <atom:link href="{base}/shows/feed.xml" rel="self" type="application/rss+xml"/>
  <description>Short podcasts about AI and tech, voiced entirely by AI voices made with Voxa, right in a web browser.</description>
  <language>en</language>
  <itunes:author>Voxa</itunes:author>
  <itunes:explicit>false</itunes:explicit>
  <itunes:category text="Technology"/>
  {f'<itunes:image href="{base}{cover}"/>' if cover else ''}
{chr(10).join(items)}
</channel>
</rss>
'''
