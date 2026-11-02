"""Licence gate and credits list for the app's Rust dependencies.

    python -I app/tools/licences.py            # write app/ui/third-party.json
    python -I app/tools/licences.py --check    # CI: fail if a licence is not allowed or the file is stale

Every crate the app builds must be usable in a closed, sold app: each licence expression
must be satisfiable with permissive licences only (for "A OR B" one side is enough, for
"A AND B" both must be). GPL, AGPL and LGPL are never allowed on their own, which is how the
plan's "no GPL in the app" rule is enforced automatically. Run from anywhere; needs cargo.

The npm packages in app/package.json are build tools (the Tauri CLI) and are not shipped.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
OUT = APP / 'ui' / 'third-party.json'

ALLOWED = {
    'MIT', 'MIT-0', 'Apache-2.0', 'BSD-1-Clause', 'BSD-2-Clause', 'BSD-3-Clause', 'ISC', 'Zlib',
    '0BSD', 'Unlicense', 'CC0-1.0', 'BSL-1.0', 'Unicode-3.0', 'Unicode-DFS-2016',
    'CDLA-Permissive-2.0', 'bzip2-1.0.6',
    # Weak, file-level copyleft: fine to ship unmodified in a closed app with attribution.
    'MPL-2.0',
}


def allowed(expr: str) -> bool:
    """Evaluates an SPDX-style expression (also the old 'MIT/Apache-2.0' form)."""
    expr = expr.replace('/', ' OR ')
    tokens = re.findall(r'\(|\)|[A-Za-z0-9.+-]+', expr)
    pos = 0

    def parse_or():
        nonlocal pos
        ok = parse_and()
        while pos < len(tokens) and tokens[pos] == 'OR':
            pos += 1
            ok = parse_and() or ok
        return ok

    def parse_and():
        nonlocal pos
        ok = parse_atom()
        while pos < len(tokens) and tokens[pos] == 'AND':
            pos += 1
            ok = parse_atom() and ok
        return ok

    def parse_atom():
        nonlocal pos
        if tokens[pos] == '(':
            pos += 1
            ok = parse_or()
            pos += 1  # ')'
        else:
            ok = tokens[pos] in ALLOWED
            pos += 1
        if pos < len(tokens) and tokens[pos] == 'WITH':
            pos += 2  # exceptions such as LLVM-exception only add permissions
        return ok

    return parse_or()


def packages() -> list[dict]:
    meta = json.loads(subprocess.run(
        ['cargo', 'metadata', '--format-version', '1', '--locked'],
        cwd=APP, check=True, capture_output=True, text=True).stdout)
    ours = set(meta['workspace_members'])
    return [p for p in meta['packages'] if p['id'] not in ours]


def main(check: bool) -> None:
    bad, rows = [], []
    for p in packages():
        lic = p.get('license') or ''
        if not lic or not allowed(lic):
            bad.append(f"{p['name']} {p['version']}: {lic or 'no licence declared'}")
        rows.append({'name': p['name'], 'version': p['version'], 'license': lic,
                     'repository': p.get('repository') or ''})
    rows.sort(key=lambda r: (r['name'], r['version']))
    if bad:
        sys.exit('Licences not allowed in the app:\n  ' + '\n  '.join(bad))
    text = json.dumps(rows, indent=0, ensure_ascii=False) + '\n'
    if check:
        if not OUT.is_file() or OUT.read_text(encoding='utf-8') != text:
            sys.exit(f'{OUT} is out of date: run python -I app/tools/licences.py')
        print(f'{len(rows)} dependencies, all licences allowed; credits list up to date')
    else:
        OUT.write_text(text, encoding='utf-8')
        print(f'wrote {OUT} ({len(rows)} dependencies)')


def self_test() -> None:
    assert allowed('MIT OR Apache-2.0')
    assert allowed('MIT/Apache-2.0')
    assert allowed('MIT OR Apache-2.0 OR LGPL-2.1-or-later')
    assert allowed('(MIT OR Apache-2.0) AND Unicode-3.0')
    assert allowed('Apache-2.0 WITH LLVM-exception')
    assert not allowed('GPL-3.0-or-later')
    assert not allowed('MIT AND GPL-2.0-only')
    assert not allowed('LGPL-2.1-or-later')
    assert not allowed('AGPL-3.0 OR GPL-3.0')


if __name__ == '__main__':
    self_test()
    main('--check' in sys.argv)
