import { defineConfig } from 'vite';

// base must match the GitHub Pages subpath: /<repo>/
export default defineConfig({
  base: '/Voxa/',
  build: {
    outDir: 'dist',
    // kokoro-js ships a wasm+onnx runtime; keep it as one chunk rather than
    // splitting the worker paths, which breaks the wasm loader.
    target: 'es2022',
  },
});
