import io
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import longjobs  # noqa: E402

RATE = 24000


def wav(seconds):
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(RATE)
        w.writeframes(b'\x01\x00' * int(RATE * seconds))
    return out.getvalue()


def fake_encode(pcm, rate):
    return b'MP3:' + str(len(pcm)).encode() + b';'   # stands in for the encoder; length is checked below


class TextTest(unittest.TestCase):
    def test_paragraphs_join_lines_and_mend_hyphens(self):
        self.assertEqual(longjobs.paragraphs('one\ntwo exam-\nple\n\n\n three '), ['one two example', 'three'])

    def test_headings_start_chapters_and_stay_in_the_text(self):
        text = 'Some preface words.\n\nChapter 1\n\nIt began.\n\nChapter 2: The end\n\nIt ended.'
        chapters = longjobs.find_chapters(text, 'My Book')
        self.assertEqual([c['title'] for c in chapters], ['My Book', 'Chapter 1', 'Chapter 2: The end'])
        self.assertIn('Chapter 1', chapters[1]['text'])
        self.assertEqual(longjobs.find_chapters('No headings here at all.', '')[0]['title'], 'Full text')

    def test_chunks_respect_the_limit_and_lose_nothing(self):
        text = '\n\n'.join(f'Sentence number {i} is here. ' * 12 for i in range(20))
        chunks = longjobs.split_chunks(text, 400)
        self.assertTrue(all(len(c) <= 400 for c in chunks))
        words = lambda s: s.split()
        self.assertEqual(words(' '.join(chunks)), words(text))
        long_word = 'x' * 1000
        self.assertTrue(all(len(c) <= 400 for c in longjobs.split_chunks(long_word, 400)))
        self.assertEqual(longjobs.split_chunks('Short.'), ['Short.'])

    def test_parse_book_checks_the_request(self):
        book = longjobs.parse_book({'title': 'T', 'chapters': [{'title': 'A', 'text': 'Hello there.'}, {'text': 'More text.'}]})
        self.assertEqual([c['title'] for c in book['chapters']], ['A', 'Chapter 2'])
        self.assertEqual(book['characters'], len('Hello there.') + len('More text.'))
        for bad in ({}, {'text': '   '}, {'chapters': [{'title': 'x'}]}, {'text': 'x' * (longjobs.MAX_BOOK_CHARS + 1)}, [], {'title': 5, 'text': 'x'}):
            with self.subTest(bad), self.assertRaises(longjobs.BadJob):
                longjobs.parse_book(bad)


class Id3Test(unittest.TestCase):
    def test_tag_has_title_toc_and_chapters(self):
        tag = longjobs.id3_chapters('Book', [{'title': 'One', 'start': 0, 'end': 1.5}, {'title': 'Two', 'start': 1.5, 'end': 4}])
        self.assertEqual(tag[:3], b'ID3')
        size = (tag[6] << 21) | (tag[7] << 14) | (tag[8] << 7) | tag[9]
        self.assertEqual(size, len(tag) - 10)
        self.assertEqual(tag.count(b'CHAP'), 2)
        self.assertEqual(tag.count(b'CTOC'), 1)
        at = tag.index(b'CHAP')
        body = tag[at + 10:at + 10 + struct.unpack('>I', tag[at + 4:at + 8])[0]]
        self.assertEqual(body[:4], b'ch0\0')
        self.assertEqual(struct.unpack('>II', body[4:12]), (0, 1500))
        self.assertIn('One'.encode('utf-16-le'), tag)


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'out.mp3')

    def tearDown(self):
        self.tmp.cleanup()

    def test_audiobook_has_chapter_marks_and_pauses(self):
        book = longjobs.parse_book({'title': 'Tale', 'chapters': [{'title': 'One', 'text': 'First words.'},
                                                                  {'title': 'Two', 'text': 'Second words.'}]})
        spoken, seen = [], []
        result = longjobs.build_audiobook(book, lambda t: spoken.append(t) or wav(2), self.path,
                                          lambda f, m: seen.append((f, m)), lambda: False, encode=fake_encode)
        self.assertEqual(spoken, ['First words.', 'Second words.'])
        # chapter 1: 2 s + a 1 s chapter pause; chapter 2: 2 s and no trailing pause
        self.assertEqual([(c['title'], c['start'], c['end']) for c in result['chapters']], [('One', 0, 3.0), ('Two', 3.0, 5.0)])
        self.assertEqual(result['duration_seconds'], 5.0)
        self.assertEqual(seen[-1][0], 1.0)
        data = open(self.path, 'rb').read()
        self.assertEqual(data[:3], b'ID3')
        self.assertEqual(data.count(b'CHAP'), 2)
        self.assertFalse(os.path.exists(self.path + '.body'))

    def test_a_failed_or_cancelled_build_leaves_no_files(self):
        book = longjobs.parse_book({'text': 'Hello world.'})

        def boom(_):
            raise RuntimeError('speech failed')
        with self.assertRaises(RuntimeError):
            longjobs.build_audiobook(book, boom, self.path, lambda *_: None, lambda: False, encode=fake_encode)
        with self.assertRaises(longjobs.Cancelled):
            longjobs.build_audiobook(book, lambda t: wav(1), self.path, lambda *_: None, lambda: True, encode=fake_encode)
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_podcast_uses_each_hosts_voice_in_order(self):
        script = {'title': 'Ep', 'hosts': {'A': 'Maya', 'B': 'Leo'}, 'chapters': [
            {'title': 'Intro', 'lines': [{'speaker': 'A', 'text': 'Hi there.'}, {'speaker': 'B', 'text': 'Hello!'}]},
            {'title': 'Topic', 'lines': [{'speaker': 'A', 'text': 'Now this.'}]}]}
        calls = []
        result = longjobs.build_podcast(script, {'A': 'harper', 'B': 'adam'}, lambda t, v: calls.append((t, v)) or wav(1),
                                        self.path, lambda *_: None, lambda: False, encode=fake_encode)
        self.assertEqual(calls, [('Hi there.', 'harper'), ('Hello!', 'adam'), ('Now this.', 'harper')])
        # 1 s + 0.35 pause, 1 s + 0.35 pause; then the last line has no pause
        self.assertEqual([(c['start'], c['end']) for c in result['chapters']], [(0, 2.7), (2.7, 3.7)])
        self.assertEqual(longjobs.script_characters(script), len('Hi there.Hello!Now this.'))

    def test_real_mp3_encoding_when_soundfile_is_present(self):
        try:
            import soundfile  # noqa: F401
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest('soundfile not installed here')
        mp3 = longjobs.wav_to_mp3(wav(1))
        self.assertTrue(mp3[:2] in (b'\xff\xf3', b'\xff\xfb', b'\xff\xe3') or mp3[:3] == b'ID3', mp3[:4])
        self.assertGreater(len(mp3), 1000)


class JobsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.jobs = longjobs.LongJobs(self.tmp.name, workers=2)

    def tearDown(self):
        self.tmp.cleanup()

    def wait(self, job):
        for _ in range(200):
            if job.status in ('done', 'error'):
                return job
            time.sleep(0.02)
        self.fail('job did not finish')

    def test_success_failure_and_refunds(self):
        refunds = []

        def ok(job):
            open(job.path, 'wb').write(b'x')
            job.result = {'duration_seconds': 1}
        job = self.wait(self.jobs.submit('audiobook', 'u:1', ok, lambda: refunds.append('ok')))
        self.assertEqual((job.status, refunds), ('done', []))
        self.assertTrue(self.jobs.path_for(job.id + '.mp3'))
        self.assertEqual(job.to_json(lambda j: f'/f/{j.id}')['audio_url'], f'/f/{job.id}')

        def told(job):
            raise longjobs.JobError('Out of characters.')
        job = self.wait(self.jobs.submit('podcast', 'u:2', told, lambda: refunds.append('told')))
        self.assertEqual((job.status, job.error, refunds), ('error', 'Out of characters.', ['told']))

        def crash(job):
            raise RuntimeError('/secret/path exploded')
        job = self.wait(self.jobs.submit('podcast', 'u:3', crash, lambda: refunds.append('crash')))
        self.assertNotIn('secret', job.error)
        self.assertEqual(refunds, ['told', 'crash'])

    def test_only_the_owner_can_see_a_job_and_the_file_link_is_the_secret(self):
        job = self.wait(self.jobs.submit('audiobook', 'u:1', lambda j: (open(j.path, 'wb').write(b'x'), setattr(j, 'result', {}))))
        self.assertIsNotNone(self.jobs.get(job.id, 'u:1'))
        self.assertIsNone(self.jobs.get(job.id, 'u:2'))
        self.assertEqual(self.jobs.path_for(job.id + '.mp3'), job.path)
        self.assertIsNone(self.jobs.path_for('0' * 32 + '.mp3'))       # unknown
        self.assertIsNone(self.jobs.path_for('../' + job.id + '.mp3'))   # not a download name
        name = self.jobs.put_file(b'RIFF', 'wav')
        self.assertEqual(open(self.jobs.path_for(name), 'rb').read(), b'RIFF')

    def test_an_account_may_have_two_unfinished_jobs(self):
        gate = threading.Event()
        work = lambda job: gate.wait(5)
        a, b = self.jobs.submit('audiobook', 'u:1', work), self.jobs.submit('podcast', 'u:1', work)
        with self.assertRaises(longjobs.BadJob):
            self.jobs.submit('audiobook', 'u:1', work)
        self.jobs.submit('audiobook', 'u:2', work)   # other accounts are not affected
        gate.set()
        self.wait(a), self.wait(b)
        self.jobs.submit('audiobook', 'u:1', lambda j: None)  # finished jobs free the slots

    def test_cancel_stops_a_running_job_and_does_not_refund(self):
        refunds = []

        def slow(job):
            for _ in range(100):
                if job.cancel.is_set():
                    raise longjobs.Cancelled()
                time.sleep(0.02)
        job = self.jobs.submit('audiobook', 'u:1', slow, lambda: refunds.append(1))
        time.sleep(0.1)
        self.assertIs(self.jobs.cancel(job.id, 'u:1'), job)
        self.assertIsNone(self.jobs.cancel(job.id, 'u:2'))
        self.wait(job)
        self.assertEqual((job.status, job.error, refunds), ('error', 'Cancelled.', []))

    def test_old_files_are_deleted(self):
        old = os.path.join(self.tmp.name, 'old.mp3')
        open(old, 'wb').write(b'x')
        os.utime(old, (time.time() - 3 * 86400,) * 2)
        longjobs.LongJobs(self.tmp.name)
        self.assertFalse(os.path.exists(old))


if __name__ == '__main__':
    unittest.main()
