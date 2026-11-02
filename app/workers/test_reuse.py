"""The RE-USE packager (no torch or model needed)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'tools'))
import build_reuse_package as pkg  # noqa: E402


class Reuse(unittest.TestCase):
    def snapshot(self, d: Path, licence='OpenMDW License Agreement, version 1.1'):
        for n in ('models', 'utils', 'recipes'):
            (d / n).mkdir()
            (d / n / 'x.py').write_text('')
        for n in ('config.json', 'model.safetensors', 'README.md'):
            (d / n).write_text('{}')
        (d / 'LICENSE').write_text(licence)

    def test_builds_a_cleanup_pack_and_keeps_nvidia_notices(self):
        with tempfile.TemporaryDirectory() as t:
            src, out = Path(t) / 'src', Path(t) / 'out'
            src.mkdir()
            self.snapshot(src)
            pkg.main(src, out)
            m = json.loads((out / 'worker.json').read_text())
            self.assertEqual((m['kind'], m['family'], m['protocol']), ('cleanup', 'Voxa Clean Film', 1))
            self.assertEqual(m['command'][-2:], ['--model', 'reuse'])
            self.assertTrue((out / 'reuse' / 'model.safetensors').is_file())
            self.assertTrue((out / 'RE-USE-LICENSE').is_file())
            self.assertTrue((out / 'reuse_worker.py').is_file())

    def test_refuses_a_snapshot_with_another_licence_or_missing_files(self):
        with tempfile.TemporaryDirectory() as t:
            src = Path(t) / 'src'
            src.mkdir()
            with self.assertRaises(SystemExit):
                pkg.main(src, Path(t) / 'a')
            self.snapshot(src, licence='Some other licence')
            with self.assertRaises(SystemExit):
                pkg.main(src, Path(t) / 'b')


if __name__ == '__main__':
    unittest.main()
