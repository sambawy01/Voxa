"""Voxa podcasts: a document in, a two-host conversation script out, written by DeepSeek.

The page reads the document (PDF, PowerPoint or text) on the device and sends its text here.
This module asks DeepSeek for a script in JSON, checks it, and hands it back to the page,
which voices each line with Voxa's own voices. Writing a script takes a minute or two, longer
than a phone keeps a silent request open, so it runs as a job the page polls, like speech.

Configuration (on the server; see docs/PODCAST.md):
    DEEPSEEK_API_KEY       turns the feature on
    VOXA_PODCAST_MODEL     default deepseek-flash
"""
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid

log = logging.getLogger('voxa.podcast')

DEEPSEEK_API = os.environ.get('DEEPSEEK_API_BASE', 'https://api.deepseek.com').rstrip('/')
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
MODEL = os.environ.get('VOXA_PODCAST_MODEL', 'deepseek-flash')
TIMEOUT = 300
MAX_SOURCE = 200_000      # characters of document text sent to the model, about 50K tokens
MAX_FOCUS = 1000
LENGTHS = {5: 750, 10: 1500, 20: 3000}  # minutes -> spoken words, at about 150 a minute
TONES = {'friendly': 'warm, curious and easy-going, like two friends who enjoy the subject',
         'expert': 'sharp and analytical, like two experts briefing busy professionals',
         'lively': 'energetic and playful, with humour, but never at the expense of accuracy',
         'critical': 'constructively sceptical: they test the ideas, name weak points and risks'}
LANGUAGES = {'a': 'English', 'b': 'British English', 'e': 'Spanish', 'f': 'French',
             'h': 'Hindi', 'i': 'Italian', 'j': 'Japanese', 'p': 'Brazilian Portuguese',
             'z': 'Mandarin Chinese (simplified characters)'}
GENDERS = {'woman': 'a woman', 'man': 'a man', 'f': 'a woman', 'm': 'a man'}
MAX_LINE = 1200           # characters in one spoken turn after cleaning
JOB_TTL = 60 * 60
MAX_RUNNING = 4           # scripts being written at once, across all users


def enabled():
    return bool(DEEPSEEK_API_KEY)


class BadRequest(ValueError):
    pass


class ModelError(Exception):
    """DeepSeek failed or returned something unusable; the message is safe to show."""


def parse_request(body):
    """Check a script request. Returns a dict of clean settings or raises BadRequest."""
    if not isinstance(body, dict):
        raise BadRequest('expected a JSON object')
    source = body.get('text')
    if not isinstance(source, str) or len(source.split()) < 30:
        raise BadRequest('The document needs some text: at least a few sentences.')
    if len(source) > MAX_SOURCE:
        raise BadRequest(f'The document is too long: at most {MAX_SOURCE:,} characters.')
    focus = body.get('focus') or ''
    if not isinstance(focus, str) or len(focus) > MAX_FOCUS:
        raise BadRequest(f'The focus can be at most {MAX_FOCUS:,} characters.')
    try:
        minutes = int(body.get('minutes', 10))
    except (TypeError, ValueError):
        raise BadRequest('minutes must be 5, 10 or 20')
    if minutes not in LENGTHS:
        raise BadRequest('minutes must be 5, 10 or 20')
    tone = body.get('tone', 'friendly')
    if tone not in TONES:
        raise BadRequest(f'tone must be one of {", ".join(TONES)}')
    lang = body.get('lang', 'a')
    if lang not in LANGUAGES:
        raise BadRequest(f'unknown language {lang!r}')
    hosts = body.get('hosts') if isinstance(body.get('hosts'), dict) else {}
    names, genders = [], []
    for key, default, gender in (('a', 'Maya', 'woman'), ('b', 'Leo', 'man')):
        name = hosts.get(key, default)
        if not isinstance(name, str) or not name.strip() or len(name) > 40:
            raise BadRequest('host names must be 1 to 40 characters')
        names.append(' '.join(name.split()))
        genders.append(GENDERS.get(hosts.get(key + '_gender', gender), ''))
    if names[0].casefold() == names[1].casefold():
        raise BadRequest('the two hosts need different names')
    title = body.get('title') or ''
    if not isinstance(title, str):
        raise BadRequest('title must be text')
    return {'text': source, 'focus': focus.strip(), 'minutes': minutes, 'tone': tone,
            'lang': lang, 'hosts': names, 'genders': genders, 'title': title.strip()[:200]}


EXAMPLE = {
    'title': 'Short, catchy episode title',
    'summary': 'Two sentences on what the episode covers.',
    'chapters': [
        {'title': 'Opening', 'lines': [
            {'speaker': 'A', 'text': 'Welcome back! Today we are digging into ...'},
            {'speaker': 'B', 'text': 'And honestly, the first thing that struck me was ...'}]},
        {'title': 'Next topic', 'lines': [{'speaker': 'A', 'text': '...'}]}],
}


def build_messages(req):
    a, b = req['hosts']
    ga, gb = (f', {g}' if g else '' for g in req.get('genders', ('', '')))
    words = LENGTHS[req['minutes']]
    system = (
        'You write scripts for an audio podcast with two hosts. '
        f'Host A is {a}{ga}; host B is {b}{gb}. They call each other by name now and then, '
        'and they talk about a document the '
        'listener has shared, explaining its ideas, the reasoning and evidence behind them, '
        'how the parts connect, what is new or surprising, and what it means in practice. '
        f'Their tone is {TONES[req["tone"]]}.\n\n'
        'Rules:\n'
        '- It is a real conversation: they react to each other, ask follow-up questions, '
        'use concrete examples and analogies, and sometimes disagree. Vary turn length; '
        'most turns are one to four sentences.\n'
        '- Stay faithful to the document. Never invent figures, names, quotes or results it '
        'does not contain. When they add general background, make that clear.\n'
        '- Everything is read aloud by a speech synthesiser, so write only the words spoken: '
        'no stage directions, no sound effects, no emojis, no markdown, no bullet points, no '
        'URLs. Write numbers, symbols and abbreviations the way they should be said.\n'
        '- Open with a short welcome that names the topic; close with the key takeaways and '
        'a goodbye. Split the episode into 3 to 8 chapters with short titles.\n'
        f'- The whole script is about {words} words (about {req["minutes"]} minutes).\n'
        f'- Write everything, titles included, in {LANGUAGES[req["lang"]]}.\n\n'
        'Reply with json only, exactly in this shape, with speaker "A" or "B":\n'
        + json.dumps(EXAMPLE, ensure_ascii=False))
    user = ''
    if req['title']:
        user += f'Document name: {req["title"]}\n'
    if req['focus']:
        user += ('The listener asked the hosts to focus on this (give it most of the time, '
                 f'but explain enough context to follow it):\n{req["focus"]}\n')
    user += f'\nThe document:\n<<<\n{req["text"]}\n>>>'
    return [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]


def call_model(messages, max_tokens):
    """One chat completion in JSON mode; returns the parsed object."""
    payload = {'model': MODEL, 'messages': messages, 'max_tokens': max_tokens,
               'response_format': {'type': 'json_object'},
               'thinking': {'type': 'disabled'}, 'temperature': 1.1, 'stream': False}
    req = urllib.request.Request(f'{DEEPSEEK_API}/chat/completions',
                                 data=json.dumps(payload).encode(), headers={
                                     'Authorization': f'Bearer {DEEPSEEK_API_KEY}',
                                     'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            reply = json.load(r)
    except urllib.error.HTTPError as e:
        log.warning('DeepSeek %s: %s', e.code, e.read()[:500])
        if e.code == 402:
            raise ModelError('The script writer is out of credit. Please tell the site owner.')
        if e.code in (429, 503):
            raise ModelError('The script writer is busy. Please try again in a minute.')
        raise ModelError(f'The script writer failed ({e.code}).')
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        log.warning('DeepSeek request failed: %s', e)
        raise ModelError('Could not reach the script writer. Please try again.')
    try:
        choice = reply['choices'][0]
        content = choice['message'].get('content') or ''
        usage = reply.get('usage') or {}
        log.info('podcast script: %s tokens in, %s out, finish %s', usage.get('prompt_tokens'),
                 usage.get('completion_tokens'), choice.get('finish_reason'))
        return json.loads(content)
    except (KeyError, IndexError, TypeError, ValueError):
        raise ModelError('The script came back incomplete. Please try again.')


STAGE = re.compile(r'\[[^\]]{0,60}\]|\([^)]*(?:laugh|chuckl|sigh|pause|music|sound)[^)]*\)|\*[^*]{0,60}\*',
                   re.IGNORECASE)


def clean_line(text):
    """What a synthesiser should read: no stage directions, markdown or links."""
    text = STAGE.sub(' ', text)
    text = re.sub(r'https?://\S+', ' ', text)
    text = re.sub(r'[#*_`>|~]+', ' ', text)
    return ' '.join(text.split())[:MAX_LINE]


def check_script(raw, hosts):
    """Validate the model's JSON. Returns {title, summary, hosts, chapters} or raises ModelError."""
    if not isinstance(raw, dict) or not isinstance(raw.get('chapters'), list):
        raise ModelError('The script came back in the wrong shape. Please try again.')
    chapters = []
    for ch in raw['chapters']:
        if not isinstance(ch, dict) or not isinstance(ch.get('lines'), list):
            continue
        lines = []
        for line in ch['lines']:
            if not isinstance(line, dict) or not isinstance(line.get('text'), str):
                continue
            speaker = str(line.get('speaker', '')).strip().upper()[:1]
            text = clean_line(line['text'])
            if speaker in ('A', 'B') and text:
                lines.append({'speaker': speaker, 'text': text})
        if lines:
            title = ch.get('title') if isinstance(ch.get('title'), str) else ''
            chapters.append({'title': clean_line(title)[:120] or f'Part {len(chapters) + 1}',
                             'lines': lines})
    if not chapters or sum(len(c['lines']) for c in chapters) < 4:
        raise ModelError('The script came back nearly empty. Please try again.')
    str_or = lambda v, d: clean_line(v) if isinstance(v, str) and v.strip() else d
    return {'title': str_or(raw.get('title'), 'Voxa podcast')[:200],
            'summary': str_or(raw.get('summary'), '')[:600],
            'hosts': {'A': hosts[0], 'B': hosts[1]}, 'chapters': chapters}


def write_script(req, call=call_model):
    words = LENGTHS[req['minutes']]
    # Tokens per word run well above one in some languages (Hindi, Japanese, Chinese).
    max_tokens = min(32000, int(words * 4) + 1000)
    messages = build_messages(req)
    try:
        return check_script(call(messages, max_tokens), req['hosts'])
    except ModelError as first:
        # JSON mode occasionally returns empty or cut-off content; one retry fixes most.
        log.info('retrying podcast script: %s', first)
        return check_script(call(messages, max_tokens), req['hosts'])


class ScriptJob:
    def __init__(self, req, owner):
        self.id = uuid.uuid4().hex
        self.req, self.owner = req, owner
        self.status, self.error, self.script = 'running', None, None
        self.created = time.time()
        self.done = threading.Event()

    def to_json(self):
        out = {'id': self.id, 'status': self.status}
        if self.status == 'done':
            out['script'] = self.script
        if self.status == 'error':
            out['error'] = self.error
        return out


class Busy(Exception):
    pass


class ScriptJobs:
    """Scripts being written, each on its own thread; finished ones expire after JOB_TTL."""

    def __init__(self, writer=write_script, max_running=MAX_RUNNING):
        self.writer, self.max_running = writer, max_running
        self._jobs, self._lock = {}, threading.Lock()

    def submit(self, req, owner, refund=lambda: None):
        with self._lock:
            now = time.time()
            for k in [k for k, j in self._jobs.items() if j.done.is_set() and now - j.created > JOB_TTL]:
                del self._jobs[k]
            if sum(not j.done.is_set() for j in self._jobs.values()) >= self.max_running:
                raise Busy()
            job = ScriptJob(req, owner)
            self._jobs[job.id] = job
        threading.Thread(target=self._run, args=(job, refund), daemon=True).start()
        return job

    def _run(self, job, refund):
        try:
            job.script = self.writer(job.req)
            job.status = 'done'
        except ModelError as e:
            job.status, job.error = 'error', str(e)
            refund()
        except Exception:
            log.exception('podcast script %s failed', job.id)
            job.status, job.error = 'error', 'Writing the script failed. Please try again.'
            refund()
        finally:
            job.req = None  # the document text is not kept once the script is written
            job.done.set()

    def get(self, job_id, owner):
        job = self._jobs.get(job_id)
        # Only the person who asked can read a script; ids are not shared secrets.
        return job if job and job.owner == owner else None
