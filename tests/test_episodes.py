"""Voxa Shows (episodes.py): episodes on the data volume, uploads by the owner, the feed.
    python -m unittest discover -s tests
"""
import io
import os
import sys
import tempfile
import unittest
import xml.dom.minidom

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import episodes  # noqa: E402

META = {'title': 'Your AI Is Lying', 'series': 'AI Explained', 'number': 1, 'description': 'Why <AI> makes things up & more',
        'published': '2026-11-22', 'duration': 185, 'hosts': ['Wren', 'Calvin'], 'made_in': '42 seconds',
        'transcript': [{'speaker': 'Calvin', 'text': 'Hot take.', 'part': 'Cold open', 'start': 2.2, 'end': 4.0}],
        'secret': 'dropped'}


class EpisodesTest(unittest.TestCase):
    def setUp(self):
        self.e = episodes.Episodes(tempfile.mkdtemp())

    def test_publish_list_and_files(self):
        ep = self.e.put_meta('ai-explained-01', META)
        self.assertNotIn('secret', ep)                       # unknown fields are dropped
        self.assertIsNone(ep['audio'])
        self.e.put_file('ai-explained-01', 'audio.mp3', io.BytesIO(b'ID3' + b'x' * 97), 100)
        self.e.put_file('ai-explained-01', 'cover.jpg', io.BytesIO(b'\xff\xd8' + b'x' * 8), 10)
        self.e.put_file('ai-explained-01', 'share.jpg', io.BytesIO(b'\xff\xd8' + b'y' * 8), 10)
        [ep] = self.e.list()
        self.assertEqual(ep['share'], '/episodes/ai-explained-01/share.jpg')     # the wide link-preview image
        self.assertEqual((ep['audio'], ep['cover'], ep['audio_bytes']), ('/episodes/ai-explained-01/audio.mp3', '/episodes/ai-explained-01/cover.jpg', 100))
        path, ctype = self.e.file('ai-explained-01', 'audio.mp3')
        self.assertEqual(ctype, 'audio/mpeg')
        self.assertIsNone(self.e.file('ai-explained-01', '../episode.json'))

    def test_mistakes_are_refused(self):
        for slug in ('../x', 'Upper', '', 'a' * 70):
            with self.assertRaises(ValueError):
                self.e.put_meta(slug, META)
        with self.assertRaises(ValueError):
            self.e.put_meta('ok', {'description': 'no title'})
        with self.assertRaises(ValueError):
            self.e.put_file('missing', 'audio.mp3', io.BytesIO(b'x'), 1)        # meta first
        self.e.put_meta('ok', META)
        with self.assertRaises(ValueError):
            self.e.put_file('ok', 'evil.sh', io.BytesIO(b'x'), 1)
        with self.assertRaises(ValueError):
            self.e.put_file('ok', 'audio.mp3', io.BytesIO(b'short'), 100)      # ended early

    def test_drafts_are_hidden(self):
        self.e.put_meta('draft-one', {**META, 'draft': True})
        self.assertEqual(self.e.list(), [])
        self.assertIsNone(self.e.file('draft-one', 'audio.mp3'))

    def test_feed(self):
        self.e.put_meta('ai-explained-01', META)
        self.e.put_file('ai-explained-01', 'audio.mp3', io.BytesIO(b'x' * 100), 100)
        rss = self.e.rss('https://voxa.test')
        xml.dom.minidom.parseString(rss)                       # valid XML, with the title escaped
        self.assertIn('https://voxa.test/episodes/ai-explained-01/audio.mp3', rss)
        self.assertIn('<itunes:duration>3:05</itunes:duration>', rss)
        self.assertIn('Why &lt;AI&gt; makes things up &amp; more', rss)

    def test_delete(self):
        self.e.put_meta('gone', META)
        self.assertTrue(self.e.delete('gone'))
        self.assertFalse(self.e.delete('gone'))


if __name__ == '__main__':
    unittest.main()
