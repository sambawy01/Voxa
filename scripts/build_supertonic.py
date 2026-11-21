"""Download Supertonic 3 and repack it for the browser.

    python scripts/build_supertonic.py [static-dir]      # default: ./static

Supertonic 3 (Supertone Inc.; weights under the BigScience OpenRAIL-M licence, code MIT) speaks
31 languages from raw text: no phonemizer. The released ONNX files are float32, 398 MB. Here:
  - every large Conv/MatMul/Gemm weight is stored as int8, per output channel, with a
    DequantizeLinear node in front of it. The maths stays float32, so the voice is unchanged
    (Whisper transcribes Arabic, French, German and English identically to the float32 build),
    while the download drops to about 110 MB. Full dynamic int8 (activations too) was tried and
    turns every module but the duration predictor into noise, so it is not used.
  - the ten voice styles become one flat float32 file plus a JSON index.
  - the licence travels with the files (OpenRAIL-M asks for that), as LICENSE.txt.

Every input file is pinned by revision and SHA-256. Needs numpy and onnx.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request

import numpy as np

REPO = 'Supertone/supertonic-3'
REVISION = '3cadd1ee6394adea1bd021217a0e650ede09a323'
DIR = 'supertonic-3-' + REVISION[:7]
MODELS = ('duration_predictor', 'text_encoder', 'vector_estimator', 'vocoder')
VOICES = ('F1', 'F2', 'F3', 'F4', 'F5', 'M1', 'M2', 'M3', 'M4', 'M5')
FILES = {
    'onnx/duration_predictor.onnx': 'c3eb91414d5ff8a7a239b7fe9e34e7e2bf8a8140d8375ffb14718b1c639325db',
    'onnx/text_encoder.onnx': 'c7befd5ea8c3119769e8a6c1486c4edc6a3bc8365c67621c881bbb774b9902ff',
    'onnx/tts.json': '42078d3aef1cd43ab43021f3c54f47d2d75ceb4e75f627f118890128b06a0d09',
    'onnx/unicode_indexer.json': '9bf7346e43883a81f8645c81224f786d43c5b57f3641f6e7671a7d6c493cb24f',
    'onnx/vector_estimator.onnx': '883ac868ea0275ef0e991524dc64f16b3c0376efd7c320af6b53f5b780d7c61c',
    'onnx/vocoder.onnx': '085de76dd8e8d5836d6ca66826601f615939218f90e519f70ee8a36ed2a4c4ba',
    'voice_styles/F1.json': 'bbdec6ee00231c2c742ad05483df5334cab3b52fda3ba38e6a07059c4563dbc2',
    'voice_styles/F2.json': '7c722c6a72707b1a77f035d67f0d1351ba187738e06f7683e8c72b1df3477fc6',
    'voice_styles/F3.json': '12f6ef2573baa2defa1128069cb59f203e3ab67c92af77b42df8a0e3a2f7c6ab',
    'voice_styles/F4.json': 'c2fa764c1225a76dfc3e2c73e8aa4f70d9ee48793860eb34c295fff01c2e032b',
    'voice_styles/F5.json': '45966e73316415626cf41a7d1c6f3b4c70dbc1ba2bee5c1978ef0ce33244fc8d',
    'voice_styles/M1.json': 'e35604687f5d23694b8e91593a93eec0e4eca6c0b02bb8ed69139ab2ea6b0a5b',
    'voice_styles/M2.json': 'b76cbf62bac707c710cf0ae5aba5e31eea1a6339a9734bfae33ab98499534a50',
    'voice_styles/M3.json': 'ea1ac35ccb91b0d7ecad533a2fbd0eec10c91513d8951e3b25fbba99954e159b',
    'voice_styles/M4.json': 'ca8eefad4fcd989c9379032ff3e50738adc547eeb5e221b82593a6d7b3bac303',
    'voice_styles/M5.json': 'dd22b92740314321f8ae11c5e87f8dd60d060f15dd3a632b5adf77f471f77af2',
    'LICENSE': '0d944a9110fed9a9602d60e0423a272903e7bd21ab060490774efc77c2275e9f',
}
QUANT_OPS = ('Conv', 'ConvTranspose', 'MatMul', 'Gemm')


def fetch(name, dest):
    url = f'https://huggingface.co/{REPO}/resolve/{REVISION}/{name}'
    path = os.path.join(dest, name.replace('/', '_'))
    with urllib.request.urlopen(url, timeout=600) as r, open(path, 'wb') as out:
        while chunk := r.read(1 << 20):
            out.write(chunk)
    with open(path, 'rb') as f:
        if hashlib.file_digest(f, 'sha256').hexdigest() != FILES[name]:
            sys.exit(f'{name}: checksum mismatch, refusing to build')
    return path


def weight_only_int8(src, dst):
    """Store large weights of Conv/MatMul/Gemm as symmetric per-channel int8 + DequantizeLinear."""
    import onnx
    from onnx import helper, numpy_helper
    m = onnx.load(src)
    if not any(o.domain in ('', 'ai.onnx') and o.version >= 13 for o in m.opset_import):
        sys.exit(f'{src}: opset below 13 has no per-axis DequantizeLinear')
    g = m.graph
    users = {}
    for n in g.node:
        for i, name in enumerate(n.input):
            users.setdefault(name, []).append((n, i))
    added, count = [], 0
    for t in list(g.initializer):
        us = users.get(t.name, [])
        if not us or any(n.op_type not in QUANT_OPS or i != 1 for n, i in us):
            continue
        w = numpy_helper.to_array(t)
        if w.dtype != np.float32 or w.size < 4096:
            continue
        op = us[0][0]
        if op.op_type == 'Conv':
            axis = 0
        elif op.op_type == 'ConvTranspose':
            axis = 1
        elif op.op_type == 'Gemm':
            axis = 0 if any(a.name == 'transB' and a.i for a in op.attribute) else 1
        else:
            axis = w.ndim - 1
        scale = np.abs(w).max(axis=tuple(a for a in range(w.ndim) if a != axis)) / 127.0
        scale[scale == 0] = 1e-8
        shape = [1] * w.ndim
        shape[axis] = -1
        q = np.clip(np.round(w / scale.reshape(shape)), -127, 127).astype(np.int8)
        g.initializer.remove(t)
        g.initializer.extend([numpy_helper.from_array(q, t.name + '_q'),
                              numpy_helper.from_array(scale.astype(np.float32), t.name + '_s'),
                              numpy_helper.from_array(np.zeros(scale.shape, np.int8), t.name + '_z')])
        added.append(helper.make_node('DequantizeLinear', [t.name + '_q', t.name + '_s', t.name + '_z'],
                                      [t.name], axis=axis, name=t.name + '_dq'))
        count += 1
    for n in reversed(added):
        g.node.insert(0, n)
    onnx.checker.check_model(m)
    onnx.save(m, dst)
    return count


def pack_voices(paths, out):
    """All voice styles in one float32 file: per voice, style_ttl then style_dp."""
    index, at = {'voices': {}}, 0
    with open(os.path.join(out, 'voices.bin'), 'wb') as f:
        for v in VOICES:
            with open(paths[v], encoding='utf-8') as j:
                s = json.load(j)
            ttl = np.asarray(s['style_ttl']['data'], np.float32).ravel()
            dp = np.asarray(s['style_dp']['data'], np.float32).ravel()
            if list(s['style_ttl']['dims']) != [1, 50, 256] or list(s['style_dp']['dims']) != [1, 8, 16]:
                sys.exit(f'{v}: unexpected style shape')
            f.write(ttl.tobytes() + dp.tobytes())
            index['voices'][v] = {'offset': at, 'ttl': len(ttl), 'dp': len(dp)}
            at += len(ttl) + len(dp)
    index['ttl_dims'], index['dp_dims'] = [1, 50, 256], [1, 8, 16]
    with open(os.path.join(out, 'voices.json'), 'w', encoding='utf-8') as f:
        json.dump(index, f)


def main(static_dir):
    out = os.path.join(static_dir, DIR)
    if os.path.isdir(out):
        print(f'{out}: already built')
        return
    with tempfile.TemporaryDirectory() as tmp:
        got = {name: fetch(name, tmp) for name in FILES}
        build = os.path.join(tmp, DIR)
        os.makedirs(build)
        for m in MODELS:
            n = weight_only_int8(got[f'onnx/{m}.onnx'], os.path.join(build, f'{m}.onnx'))
            print(f'{m}: {n} weights to int8, {os.path.getsize(os.path.join(build, m + ".onnx")) / 1e6:.1f} MB')
        shutil.copy(got['onnx/tts.json'], os.path.join(build, 'tts.json'))
        shutil.copy(got['onnx/unicode_indexer.json'], os.path.join(build, 'unicode_indexer.json'))
        shutil.copy(got['LICENSE'], os.path.join(build, 'LICENSE.txt'))
        pack_voices({v: got[f'voice_styles/{v}.json'] for v in VOICES}, build)
        with open(os.path.join(build, 'SOURCE.txt'), 'w', encoding='utf-8') as f:
            f.write(f'Supertonic 3 by Supertone Inc. (https://huggingface.co/{REPO}, revision {REVISION}).\n'
                    'Model weights: BigScience OpenRAIL-M (see LICENSE.txt), including its use restrictions.\n'
                    'Repacked for Voxa: weights stored as int8 per channel (computed in float32); '
                    'voice styles packed into voices.bin.\n')
        os.makedirs(static_dir, exist_ok=True)
        shutil.move(build, out)
    total = sum(os.path.getsize(os.path.join(out, f)) for f in os.listdir(out))
    print(f'{out}: {total / 1e6:.0f} MB')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'static'))
