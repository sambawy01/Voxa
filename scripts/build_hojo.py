"""Download Hojo TTS Light 40M and repack it for the browser.

    python scripts/build_hojo.py [static-dir]      # default: ./static

Hojo TTS Light 40M (Apache-2.0, HojoAI) speaks English and Chinese. The released ONNX files
store weights in bfloat16, which onnxruntime-web cannot run, so this widens them to float32
and then:
  - the language model and the fine stage are quantized to int8 (per channel, MatMul/Gemm
    only), which keeps them small to download and small in a phone's memory;
  - the audio decoder keeps its float weights, stored as float16 and cast back on load,
    because int8 there adds audible roughness;
  - the voice bank (.npz) becomes one flat binary file plus a JSON description, since the
    browser has no NumPy: token embeddings as float16, speaker embeddings as float32.
The tokenizer is reduced to the BPE vocabulary and merges that text needs.

Every input file is pinned by revision and SHA-256. Needs numpy, onnx and onnxruntime.
"""
import ast
import hashlib
import io
import json
import os
import pickletools
import re
import sys
import tempfile
import urllib.request
import zipfile

import numpy as np

REPO = 'HojoAI/Hojo-TTS-Light-40M'
REVISION = 'c3cf21c77dc04b220aa4dabe20fc59e66a2a5485'
DIR = 'hojo-40m-' + REVISION[:7]
FILES = {
    'Hojo-TTS-Light-40M-llm.onnx': '8835d852eb3f9fb7261c917cba4252d1d761a3a7c0849e488b6af7535271e211',
    'Hojo-TTS-Light-40M-fine_local.onnx': '61adef3b3beac3e692c1af04a129f397ec8c742db64a968235e5ff8473b7e324',
    'Hojo-TTS-Light-40M-decoder.onnx': 'f66b86ecbc5cd619e0fd223c06a43e7838ee6af3c42e3fcd41ab68591e05e2e8',
    'Hojo-TTS-Light-40M-voice.npz': '96cf5d470d6910fb8e0bebb5d38799d948e604746ff6ddeddb05913cab834483',
    'config.json': '77e57b7fea1f1125171a28455090a05a1227cf5850c859697d81f5dc13a335ce',
    'tokenizer.json': 'f4ad802b750ab45534bfd05fade29fc749960761afb70b80b5b99156bc3eef7a',
}
SPECIAL = ['[target_text_start]', '[target_text_end]', '[spk_start]', '[spk_end]',
           '[target_speech_start]', '[target_speech_end]']
HOP, N_FFT = 480, 1920  # the codec's ISTFT; the release has no codec_meta.json to override it


def fetch(name, dest):
    url = f'https://huggingface.co/{REPO}/resolve/{REVISION}/{name}'
    path = os.path.join(dest, name)
    with urllib.request.urlopen(url, timeout=600) as r, open(path, 'wb') as out:
        while chunk := r.read(1 << 20):
            out.write(chunk)
    with open(path, 'rb') as f:
        if hashlib.file_digest(f, 'sha256').hexdigest() != FILES[name]:
            sys.exit(f'{name}: checksum mismatch, refusing to build')
    return path


def widen_bf16(model):
    """Rewrite bfloat16 weights, types and casts as float32 (lossless)."""
    from onnx import AttributeProto, TensorProto, numpy_helper

    def fix(t):
        if t.data_type == TensorProto.BFLOAT16:
            t.CopyFrom(numpy_helper.from_array(numpy_helper.to_array(t).astype(np.float32), t.name))

    for t in model.graph.initializer:
        fix(t)
    for vi in [*model.graph.input, *model.graph.output, *model.graph.value_info]:
        if vi.type.tensor_type.elem_type == TensorProto.BFLOAT16:
            vi.type.tensor_type.elem_type = TensorProto.FLOAT
    for node in model.graph.node:
        for a in node.attribute:
            if node.op_type == 'Cast' and a.name == 'to' and a.i == TensorProto.BFLOAT16:
                a.i = TensorProto.FLOAT
            if a.type == AttributeProto.TENSOR:
                fix(a.t)
            elif a.type == AttributeProto.TENSORS:
                for t in a.tensors:
                    fix(t)
    del model.graph.value_info[:]
    return model


def half_weights(model):
    """Store large float32 weights as float16 with a Cast back to float32 at load time."""
    from onnx import TensorProto, helper, numpy_helper

    graph, casts = model.graph, []
    for t in list(graph.initializer):
        if t.data_type == TensorProto.FLOAT and int(np.prod(t.dims)) >= 1024:
            half = numpy_helper.from_array(numpy_helper.to_array(t).astype(np.float16), t.name + '__fp16')
            graph.initializer.remove(t)
            graph.initializer.append(half)
            casts.append(helper.make_node('Cast', [half.name], [t.name], to=TensorProto.FLOAT))
    for c in reversed(casts):
        graph.node.insert(0, c)
    return model


def voice_ids(pickled, count):
    """The strings of a pickled 1-d object array: the ones inside its list, read by opcode."""
    found, current = [], None
    for op, arg, _ in pickletools.genops(pickled):
        if op.name == 'EMPTY_LIST':
            current = []
        elif current is not None and op.name in ('SHORT_BINUNICODE', 'BINUNICODE', 'BINUNICODE8'):
            current.append(arg)
        elif current is not None and op.name == 'APPENDS':
            found.append(current)
            current = None
    matches = [ids for ids in found if len(ids) == count]
    if len(matches) != 1:
        sys.exit('voice_ids is not a plain list of strings')
    return matches[0]


def read_npz(path):
    """Numeric arrays from the voice bank, plus voice_ids, without unpickling anything."""
    out = {}
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            raw = z.read(info)
            major = raw[6]
            hl, at = (int.from_bytes(raw[8:10], 'little'), 10) if major == 1 else (int.from_bytes(raw[8:12], 'little'), 12)
            header = ast.literal_eval(raw[at:at + hl].decode('latin1'))
            key = info.filename[:-4]
            if 'O' in str(header['descr']):
                if key != 'voice_ids':
                    sys.exit(f'{key}: pickled array refused')
                out[key] = voice_ids(raw[at + hl:], header['shape'][0])
            else:
                out[key] = np.load(io.BytesIO(raw), allow_pickle=False)
    return out


def main(root):
    import onnx
    from onnxruntime.quantization import QuantType, quantize_dynamic

    dest = os.path.join(root, DIR)
    os.makedirs(dest, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = {name: fetch(name, tmp) for name in FILES}
        for part, out in [('llm', 'llm.onnx'), ('fine_local', 'fine.onnx')]:
            wide = os.path.join(tmp, part + '-fp32.onnx')
            onnx.save(widen_bf16(onnx.load(src[f'Hojo-TTS-Light-40M-{part}.onnx'])), wide)
            quantize_dynamic(wide, os.path.join(dest, out), weight_type=QuantType.QInt8,
                             op_types_to_quantize=['MatMul', 'Gemm'], per_channel=True)
        onnx.save(half_weights(widen_bf16(onnx.load(src['Hojo-TTS-Light-40M-decoder.onnx']))),
                  os.path.join(dest, 'decoder.onnx'))

        bank = read_npz(src['Hojo-TTS-Light-40M-voice.npz'])
        config = json.load(open(src['config.json']))
        tok = json.load(open(src['tokenizer.json'], encoding='utf-8'))

    ids = bank['voice_ids']
    emb = np.asarray(bank['token_embedding'], np.float32)
    spk = np.asarray(bank['speaker_embeds'], np.float32)
    vec = np.asarray(bank['speaker_vecs'], np.float32)
    if len(ids) != spk.shape[0] or len(ids) != vec.shape[0]:
        sys.exit('voice bank shapes disagree')
    parts = [emb.astype('<f2').tobytes(), spk.astype('<f4').tobytes(), vec.astype('<f4').tobytes()]
    with open(os.path.join(dest, 'voices.bin'), 'wb') as f:
        for p in parts:
            f.write(p)

    added = {t['content']: t['id'] for t in tok['added_tokens']}
    audio = sorted((int(m.group(1)), i) for c, i in added.items() if (m := re.fullmatch(r'\[(\d+)\]', c)))
    if [i for _, i in audio] != list(range(audio[0][1], audio[0][1] + len(audio))) or audio[0][0] != 0:
        sys.exit('audio tokens are not one contiguous run')
    if tok['normalizer']['normalizers'][0]['type'] != 'Lowercase' or tok['model']['type'] != 'BPE':
        sys.exit('tokenizer layout changed; check hojo.js before shipping')
    meta = {
        'repo': REPO, 'revision': REVISION, 'licence': 'Apache-2.0',
        'voices': ids, 'layers': config['num_hidden_layers'], 'hidden': emb.shape[1], 'vocab': emb.shape[0],
        'kv_heads': config['num_key_value_heads'], 'head_dim': config['head_dim'],
        'max_positions': config['max_position_embeddings'],
        'slots': spk.shape[1], 'speaker_dim': vec.shape[1], 'hop': HOP, 'n_fft': N_FFT, 'sample_rate': 24000,
        'audio_first': audio[0][1], 'audio_count': len(audio), 'unk': added['[UNK]'],
        'special': {name: added[name] for name in SPECIAL},
        'speaker_slots': [added[f'[spk_emb_{i}]'] for i in range(spk.shape[1])],
        'layout': {'token_embedding': [0, 'float16'], 'speaker_embeds': [len(parts[0]), 'float32'],
                   'speaker_vecs': [len(parts[0]) + len(parts[1]), 'float32']},
        'vocabulary': tok['model']['vocab'],
        'merges': [m if isinstance(m, str) else ' '.join(m) for m in tok['model']['merges']],
    }
    with open(os.path.join(dest, 'hojo.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, separators=(',', ':'))
    for name in sorted(os.listdir(dest)):
        print(f'{name}: {os.path.getsize(os.path.join(dest, name)) / 1e6:.1f} MB')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static'))
