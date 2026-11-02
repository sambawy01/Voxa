"""The Studio worker and its packaging agree on voices (no model or torch needed)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'tools'))
import build_studio_package as pkg  # noqa: E402
import qwen3_tts_worker as worker  # noqa: E402


class Studio(unittest.TestCase):
    def test_voice_lists_match(self):
        self.assertEqual([v[0] for v in pkg.VOICES], list(worker.VOICES))
        langs = {'english': 'en-US', 'chinese': 'zh', 'japanese': 'ja', 'korean': 'ko'}
        for vid, _, _, tag in pkg.VOICES:
            self.assertEqual(langs[worker.VOICES[vid][1]], tag, vid)

    def test_manifest_runs_the_bundled_env_or_python(self):
        m = pkg.manifest(['python3', '-I', 'qwen3_tts_worker.py'])
        self.assertEqual((m['protocol'], m['family']), (1, 'Voxa Studio'))
        self.assertEqual(len(m['voices']), len(worker.VOICES))
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / 'env'
            (env / 'bin').mkdir(parents=True)
            (env / 'bin' / 'python3').write_text('')
            self.assertEqual(pkg.env_python(env), 'bin/python3')

    def test_refuses_a_folder_that_is_not_the_model(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit):
                pkg.main(Path(d), Path(d) / 'out')


if __name__ == '__main__':
    unittest.main()
