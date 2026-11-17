"""Audiobooks and podcasts made on the server, for the API: long jobs, and the audio they build.

The website builds these in the browser. An agent has no browser, so the same steps run here:
text is split into chapters and chunks (the rules of web/book.js), each chunk is spoken by
the ordinary speech queue (so the GPU, the cache and the queue limits all apply), and the
audio is joined into one MP3 with a chapter mark per chapter.

Nothing in this module knows about HTTP or accounts. The server passes in what it needs:
`speak(text, voice)` returns a WAV, `charge(chars, voice_class)` takes characters from the
caller's plan and returns a refund callable, and so on, which also makes it testable without a
speech model.
"""
import io
import logging
import os
import re
import struct
import threading
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger('voxa.longjobs')

CHUNK_CHARS = 1500         # as web/book.js: a chunk is a paragraph or two
CHUNK_PAUSE = 0.4          # seconds of silence between chunks of a chapter
CHAPTER_PAUSE = 1.0        # and between chapters
TURN_PAUSE = 0.35          # between speakers of a podcast, as web/podcast.js
MAX_BOOK_CHARS = 400_000   # about 7 hours of speech
MAX_CHAPTERS = 300
MAX_TITLE = 200
OUTPUT_TTL = 24 * 3600     # finished files are kept this long
OUTPUT_CAP_BYTES = 2 * 1024 ** 3
JOB_TTL = 24 * 3600
MAX_RUNNING = int(os.environ.get('VOXA_LONG_JOBS', '3'))   # building at once, across all accounts
MAX_WAITING = 20
PER_ACCOUNT = 2            # unfinished long jobs one account may have
MP3_LEVEL = 0.6            # libsndfile constant-bitrate quality: about 64 kbit/s at 24 kHz

HEADING = re.compile(r'^(chapter|part|book|prologue|epilogue|introduction|preface|foreword|afterword|appendix)\b[^\n]{0,70}$', re.I)


class BadJob(ValueError):
    """A request that cannot be done; the message is safe to show."""


class JobError(Exception):
    """A job that failed in a way worth telling the caller about."""


class Cancelled(Exception):
    pass


# --- text: paragraphs, chapters, chunks ------------------------------------------------------

def paragraphs(text):
    """Blank lines separate paragraphs; single newlines inside one are joined."""
    out = []
    for block in re.split(r'\n\s*\n', text.replace('\r\n', '\n').replace('\r', '\n')):
        joined = ''
        for line in block.split('\n'):
            line = line.strip().replace('­', '')
            if not line:
                continue
            if not joined:
                joined = line
            elif re.search(r'[A-Za-zÀ-ÿ]-$', joined) and re.match(r'[a-zà-ÿ]', line):
                joined = joined[:-1] + line
            else:
                joined += ' ' + line
        if joined.strip():
            out.append(joined.strip())
    return out


def find_chapters(text, title=''):
    """[{title, text}] from plain text. Lines like "Chapter 3" or "Part II" start a chapter and
    stay in its text, so it is announced when read. Text before the first heading is kept as
    its own chapter. No headings at all: the whole text is one chapter."""
    chapters = []
    for para in paragraphs(text):
        if HEADING.match(para) or not chapters:
            chapters.append({'title': para if HEADING.match(para) else (title or 'Opening'), 'paras': []})
        chapters[-1]['paras'].append(para)
    if len(chapters) == 1:
        chapters[0]['title'] = title or 'Full text'
    return [{'title': c['title'], 'text': '\n\n'.join(c['paras'])} for c in chapters if c['paras']]


def split_chunks(text, limit=CHUNK_CHARS):
    """Chunks of at most `limit` characters, cut at paragraphs, then sentences, then words."""
    pieces = []
    for para in (p.strip() for p in re.split(r'\n\s*\n', text)):
        if not para:
            continue
        if len(para) <= limit:
            pieces.append(para)
            continue
        for sentence in re.split(r'(?<=[.!?…]["”’)\]])\s+|(?<=[.!?…])\s+', para):
            while len(sentence) > limit:
                cut = sentence.rfind(' ', 0, limit)
                cut = cut if cut > limit * 0.5 else limit
                pieces.append(sentence[:cut].strip())
                sentence = sentence[cut:].strip()
            if sentence:
                pieces.append(sentence)
    chunks = []
    for piece in pieces:
        if chunks and len(chunks[-1]) + 2 + len(piece) <= limit:
            chunks[-1] += '\n\n' + piece
        else:
            chunks.append(piece)
    return chunks


def parse_book(body):
    """Check an audiobook request. Returns {title, chapters: [{title, chunks}], characters}."""
    if not isinstance(body, dict):
        raise BadJob('expected a JSON object')
    title = body.get('title') or ''
    if not isinstance(title, str) or len(title) > MAX_TITLE:
        raise BadJob(f'title must be text of at most {MAX_TITLE} characters')
    title = ' '.join(title.split())
    if isinstance(body.get('chapters'), list):
        raw = []
        for c in body['chapters']:
            if not isinstance(c, dict) or not isinstance(c.get('text'), str):
                raise BadJob('each chapter needs a "text"')
            chapter_title = c.get('title') if isinstance(c.get('title'), str) else ''
            raw.append({'title': ' '.join(chapter_title.split())[:MAX_TITLE] or f'Chapter {len(raw) + 1}',
                        'text': '\n\n'.join(paragraphs(c['text']))})
        raw = [c for c in raw if c['text']]
    elif isinstance(body.get('text'), str):
        raw = find_chapters(body['text'], title)
    else:
        raise BadJob('send "text", or "chapters": [{"title", "text"}]')
    if not raw:
        raise BadJob('There is no text to read.')
    if len(raw) > MAX_CHAPTERS:
        raise BadJob(f'At most {MAX_CHAPTERS} chapters.')
    chapters = [{'title': c['title'], 'chunks': split_chunks(c['text'])} for c in raw]
    characters = sum(len(k) for c in chapters for k in c['chunks'])
    if characters > MAX_BOOK_CHARS:
        raise BadJob(f'Too long for one audiobook: {characters:,} characters; the most is {MAX_BOOK_CHARS:,}. '
                     'Make it in parts.')
    return {'title': title or chapters[0]['title'], 'chapters': chapters, 'characters': characters}


# --- audio: WAV in, MP3 with chapters out ----------------------------------------------------

def wav_pcm(blob):
    """(16-bit mono PCM bytes, sample rate) of a WAV."""
    with wave.open(io.BytesIO(blob), 'rb') as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError('expected a 16-bit mono WAV')
        return w.readframes(w.getnframes()), w.getframerate()


def silence(seconds, rate):
    return b'\0\0' * int(rate * seconds)


def encode_mp3(pcm, rate):
    """MP3 bytes for 16-bit mono PCM (libsndfile, constant bitrate)."""
    import numpy as np
    import soundfile as sf
    out = io.BytesIO()
    sf.write(out, np.frombuffer(pcm, dtype='<i2'), rate, format='MP3', subtype='MPEG_LAYER_III',
             compression_level=MP3_LEVEL, bitrate_mode='CONSTANT')
    return out.getvalue()


def wav_to_mp3(blob):
    pcm, rate = wav_pcm(blob)
    return encode_mp3(pcm, rate)


def id3_chapters(title, marks):
    """An ID3v2.3 tag with the title and a chapter mark per chapter ({title, start, end} in
    seconds); what podcast and audiobook players show as chapters. As web/book.js id3Chapters."""
    def text(s):
        return b'\x01\xff\xfe' + s.encode('utf-16-le') + b'\0\0'

    def frame(frame_id, body):
        return frame_id.encode() + struct.pack('>I', len(body)) + b'\0\0' + body
    frames = frame('TIT2', text(title))
    ids = [f'ch{i}'.encode() for i in range(len(marks))]
    frames += frame('CTOC', b'toc\0\x03' + bytes([len(ids)]) + b''.join(i + b'\0' for i in ids))
    for cid, m in zip(ids, marks):
        frames += frame('CHAP', cid + b'\0' + struct.pack('>IIII', round(m['start'] * 1000), round(m['end'] * 1000),
                                                          0xFFFFFFFF, 0xFFFFFFFF) + frame('TIT2', text(m['title'])))
    n = len(frames)
    return b'ID3\x03\0\0' + bytes([(n >> 21) & 127, (n >> 14) & 127, (n >> 7) & 127, n & 127]) + frames


class Mp3Writer:
    """Builds one chapter-marked MP3 on disk without holding the book in memory: audio is
    appended chapter by chapter, and the tag (which needs every chapter's time) is put in front
    at the end."""

    def __init__(self, path):
        self.path = path
        self._body = open(path + '.body', 'wb')
        self.marks, self.seconds = [], 0.0

    def add_chapter(self, title, parts):
        """`parts` yields (mp3 bytes, seconds) in order."""
        start = self.seconds
        for data, seconds in parts:
            self._body.write(data)
            self.seconds += seconds
        self.marks.append({'title': title, 'start': start, 'end': self.seconds})

    def finish(self, title):
        self._body.close()
        with open(self.path + '.tmp', 'wb') as out, open(self.path + '.body', 'rb') as body:
            out.write(id3_chapters(title, self.marks))
            while True:
                block = body.read(1 << 20)
                if not block:
                    break
                out.write(block)
        os.remove(self.path + '.body')
        os.replace(self.path + '.tmp', self.path)
        return self.marks, self.seconds

    def abort(self):
        self._body.close()
        for suffix in ('.body', '.tmp'):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass


def pcm_seconds(pcm, rate):
    return len(pcm) / 2 / rate


# --- the builders --------------------------------------------------------------------------

def build_audiobook(book, speak, path, progress, cancelled, encode=None):
    """Speak every chunk of `book` (from parse_book) and write the MP3 to `path`.
    progress(fraction, message) reports; cancelled() says whether to stop. Returns the
    result dict."""
    encode = encode or encode_mp3
    total = book['characters']
    done = 0
    writer = Mp3Writer(path)
    try:
        for ci, chapter in enumerate(book['chapters']):
            def parts(chapter=chapter, ci=ci):
                nonlocal done
                for ki, chunk in enumerate(chapter['chunks']):
                    if cancelled():
                        raise Cancelled()
                    pcm, rate = wav_pcm(speak(chunk))
                    last = ki == len(chapter['chunks']) - 1
                    gap = CHAPTER_PAUSE if last and ci < len(book['chapters']) - 1 else (0 if last else CHUNK_PAUSE)
                    pcm += silence(gap, rate)
                    done += len(chunk)
                    progress(done / total, f'Chapter {ci + 1} of {len(book["chapters"])}')
                    yield encode(pcm, rate), pcm_seconds(pcm, rate)
            writer.add_chapter(chapter['title'], parts())
        marks, seconds = writer.finish(book['title'])
    except BaseException:
        writer.abort()
        raise
    return {'duration_seconds': round(seconds, 1), 'chapters': [
        {'title': m['title'], 'start': round(m['start'], 1), 'end': round(m['end'], 1)} for m in marks],
        'characters': total}


def script_characters(script):
    return sum(len(line['text']) for ch in script['chapters'] for line in ch['lines'])


def build_podcast(script, voices, speak, path, progress, cancelled, encode=None):
    """Voice a script from podcast.write_script. `voices` is {'A': voice, 'B': voice}."""
    encode = encode or encode_mp3
    total = script_characters(script)
    done = 0
    writer = Mp3Writer(path)
    try:
        for ci, chapter in enumerate(script['chapters']):
            def parts(chapter=chapter, ci=ci):
                nonlocal done
                for li, line in enumerate(chapter['lines']):
                    if cancelled():
                        raise Cancelled()
                    pcm, rate = wav_pcm(speak(line['text'], voices[line['speaker']]))
                    last = li == len(chapter['lines']) - 1 and ci == len(script['chapters']) - 1
                    pcm += silence(0 if last else TURN_PAUSE, rate)
                    done += len(line['text'])
                    progress(done / total, f'Recording: part {ci + 1} of {len(script["chapters"])}')
                    yield encode(pcm, rate), pcm_seconds(pcm, rate)
            writer.add_chapter(chapter['title'], parts())
        marks, seconds = writer.finish(script['title'])
    except BaseException:
        writer.abort()
        raise
    return {'duration_seconds': round(seconds, 1), 'chapters': [
        {'title': m['title'], 'start': round(m['start'], 1), 'end': round(m['end'], 1)} for m in marks],
        'characters': total}


# --- running jobs --------------------------------------------------------------------------

class LongJob:
    def __init__(self, kind, owner, directory):
        self.id = uuid.uuid4().hex
        self.kind, self.owner = kind, owner
        self.status, self.stage, self.progress = 'queued', 'Waiting its turn', 0.0
        self.error, self.result = None, None
        self.created, self.finished = time.time(), None
        self.cancel = threading.Event()
        self.path = os.path.join(directory, f'{self.id}.mp3')

    def to_json(self, file_url=None):
        out = {'id': self.id, 'kind': self.kind, 'status': self.status, 'stage': self.stage,
               'progress': round(self.progress, 3), 'created': int(self.created)}
        if self.error:
            out['error'] = self.error
        if self.result is not None:
            out.update(self.result)
        if self.status == 'done' and file_url and os.path.isfile(self.path):
            out['audio_url'] = file_url(self)
        return out


class LongJobs:
    """Runs long jobs on a few worker threads and keeps their MP3 files for a day."""

    def __init__(self, directory, workers=MAX_RUNNING, ttl=OUTPUT_TTL, clock=time.time):
        self.directory, self.ttl, self.clock = directory, ttl, clock
        os.makedirs(directory, exist_ok=True)
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='voxa-long')
        self._jobs, self._lock = {}, threading.Lock()
        self._sweep_files()

    def submit(self, kind, owner, work, refund=lambda: None):
        """Queue `work(job)`; it sets job.result (a dict) or raises JobError. Returns the job.
        Raises BadJob when the account has too many unfinished jobs or the server is busy."""
        with self._lock:
            self._purge()
            unfinished = [j for j in self._jobs.values() if j.status in ('queued', 'running')]
            if sum(j.owner == owner for j in unfinished) >= PER_ACCOUNT:
                raise BadJob(f'You already have {PER_ACCOUNT} audiobooks or podcasts being made. '
                             'Wait for one to finish (or cancel it).')
            if len(unfinished) >= MAX_RUNNING + MAX_WAITING:
                raise Busy()
            job = LongJob(kind, owner, self.directory)
            self._jobs[job.id] = job
        self._pool.submit(self._run, job, work, refund)
        return job

    def _run(self, job, work, refund):
        if job.cancel.is_set():
            return self._end(job, 'error', error='Cancelled.')
        job.status, job.stage = 'running', 'Starting'
        try:
            work(job)
            job.progress = 1.0
            self._end(job, 'done', stage='Done')
        except Cancelled:
            self._end(job, 'error', error='Cancelled.')
        except JobError as e:
            refund()
            self._end(job, 'error', error=str(e))
        except Exception:
            log.exception('long job %s (%s) failed', job.id, job.kind)
            refund()
            self._end(job, 'error', error='Making this failed on the server. Nothing was charged; please try again.')

    @staticmethod
    def _end(job, status, error=None, stage=None):
        job.status, job.error, job.finished = status, error, time.time()
        if stage:
            job.stage = stage

    def get(self, job_id, owner):
        job = self._jobs.get(job_id)
        return job if job and job.owner == owner else None

    def put_file(self, data, ext):
        """Keep some bytes as a downloadable file for the usual time. Returns its name."""
        name = f'{uuid.uuid4().hex}.{ext}'
        with open(os.path.join(self.directory, name), 'wb') as f:
            f.write(data)
        return name

    def path_for(self, name):
        """The file behind a download name (`<32 hex>.mp3` or `.wav`), or None if it is not one of
        ours or has expired. The unguessable name is the only secret."""
        if not re.fullmatch(r'[0-9a-f]{32}\.(mp3|wav)', name or ''):
            return None
        path = os.path.join(self.directory, name)
        try:
            return path if self.clock() - os.path.getmtime(path) < self.ttl else None
        except OSError:
            return None

    def cancel(self, job_id, owner):
        job = self.get(job_id, owner)
        if job and job.status in ('queued', 'running'):
            job.cancel.set()
        return job

    def _purge(self):
        now = self.clock()
        for jid in [k for k, j in self._jobs.items() if j.finished and now - j.finished > max(self.ttl, JOB_TTL)]:
            job = self._jobs.pop(jid)
            try:
                os.remove(job.path)
            except OSError:
                pass
        self._sweep_files()

    def _sweep_files(self):
        """Delete old files, and the oldest ones beyond the size cap (also after a restart)."""
        try:
            files = [(os.path.getmtime(p), os.path.getsize(p), p)
                     for p in (os.path.join(self.directory, n) for n in os.listdir(self.directory))
                     if os.path.isfile(p)]
        except OSError:
            return
        now, total = time.time(), sum(s for _, s, _ in files)
        for mtime, size, path in sorted(files):
            if now - mtime > self.ttl or total > OUTPUT_CAP_BYTES:
                try:
                    os.remove(path)
                    total -= size
                except OSError:
                    pass


class Busy(Exception):
    pass
