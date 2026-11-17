# dizione (vendored)

English text to phonemes, by Muhammed Nazeem (Apache-2.0, see LICENSE). It is the phonemizer
tiny-voca was trained with, so the browser runs this exact code under Pyodide
(scripts/build_tiny_voca.py zips it, web/tiny-voca-worker.js loads it).

- Source: https://github.com/n4ze3m/dizione, commit 8cd889e4de7be6bd1dd2e6891344a0729fa14dd4
  (the commit the authors' own browser demo pins).
- Copied unmodified from that commit's `src/dizione/`, apart from leaving out the command-line
  entry point (`__main__.py`).
- Its data files (lexicon, G2P weights) are not here: they ship with the model at
  https://huggingface.co/n4ze3m/tiny-voca (`phonemizer/`).
