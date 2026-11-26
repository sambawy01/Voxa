"""Arabic numbers read as words: the Python version (agentapi.py, the local server) and the browser
version (web/arabic-numbers.js) must both give exactly the answers in arabic_numbers_cases.json.
    python -m unittest discover -s tests
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
import agentapi  # noqa: E402

CASES = json.load(open(os.path.join(HERE, 'arabic_numbers_cases.json'), encoding='utf-8'))
JS = os.path.join(HERE, '..', 'web', 'arabic-numbers.js')


class PythonTest(unittest.TestCase):
    def test_cases(self):
        for c in CASES:
            with self.subTest(text=c['in']):
                self.assertEqual(agentapi.normalize_arabic(c['in']), c['out'])

    def test_no_digit_is_left_in_arabic_text(self):
        for c in CASES:
            if any('؀' <= ch <= 'ۿ' for ch in c['in']) and not any(x in c['in'] for x in ('mp3', 'A4', '5G', 'COVID', 'GPT', 'v2')):
                with self.subTest(text=c['in']):
                    self.assertFalse(any(ch.isdigit() for ch in agentapi.normalize_arabic(c['in'])))

    def test_idempotent(self):
        for c in CASES:
            self.assertEqual(agentapi.normalize_arabic(c['out']), c['out'])

    def test_voice_gets_words_for_arabic_only(self):
        calls, original = [], agentapi.normalize_arabic
        agentapi.normalize_arabic = lambda t: calls.append(t) or original(t)
        try:
            st = agentapi.Supertonic('/nonexistent')
            st.sessions = {}                         # no model: it fails right after the text step, which is all we look at
            for text, lang in (('سنة 1998', 'ar'), ('year 1998', 'en'), ('année 1998', 'f')):
                try:
                    st.synth(text, 'F1', lang)
                except Exception:
                    pass
        finally:
            agentapi.normalize_arabic = original
        self.assertEqual(calls, ['سنة 1998'])         # Arabic only; other languages are left alone


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class BrowserVersionTest(unittest.TestCase):
    def test_same_answers_in_the_browser_code(self):
        script = (
            "const fs=require('fs'),vm=require('vm');"
            "const src=fs.readFileSync(process.argv[1],'utf8').replace(/^export /gm,'');"
            "const ctx={};vm.createContext(ctx);vm.runInContext(src+';this.f=normalizeArabic;',ctx);"
            "const cases=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));"
            "process.stdout.write(JSON.stringify(cases.map(c=>ctx.f(c.in))));")
        out = subprocess.run(['node', '-e', script, JS, os.path.join(HERE, 'arabic_numbers_cases.json')],
                             capture_output=True, text=True, timeout=60, check=True).stdout
        got = json.loads(out)
        for c, g in zip(CASES, got):
            with self.subTest(text=c['in']):
                self.assertEqual(g, c['out'])


if __name__ == '__main__':
    unittest.main()
