# Model catalogue: format and publishing

The app learns which models exist from a signed `catalogue.json`. The code is in
`app/crates/voxa-models`; the rules come from [NATIVE_APP_VOICES.md](NATIVE_APP_VOICES.md).

## Files Voxa publishes

- `catalogue.json`: the list of models.
- `catalogue.json.sig`: a base64 Ed25519 signature over the exact bytes of `catalogue.json`.
- The model files themselves, on Voxa's own mirror (https only). Never link to upstream.

## Format (schema 1)

```json
{
  "schema": 1,
  "sequence": 12,
  "issued": "2026-11-02T00:00:00Z",
  "models": [{
    "id": "kitten-nano", "version": "0.8.0", "engine": "kitten",
    "tier": "phone", "languages": ["en"], "cloning": false,
    "license": {"spdx": "Apache-2.0", "credit": "Kitten TTS by KittenML",
                "url": "https://huggingface.co/KittenML", "restrictions": null},
    "min_app": "0.1.0",
    "requirements": {"ram_mb": 512, "vram_mb": null, "measured": false},
    "files": [{"path": "model.onnx", "url": "https://<mirror>/kitten-nano/0.8.0/model.onnx",
               "sha256": "<64 hex>", "size": 25000000}],
    "retired": false
  }]
}
```

### Fields

- **`sequence`**: must increase with every publish. The app refuses a catalogue older than
  the last one it accepted (rollback protection).
- **`tier`**: `phone`, `computer` or `gpu`. This is the device label on the voice card.
- **`requirements.measured`**: stays `false` until the device benchmark (roadmap M5) has
  measured the model.
- **`retired`**: set to `true` when upstream pulls or relicenses a model. Installed copies
  keep working, and new downloads stop.
- **`id` and `version`**: `[a-z0-9._-]`. A file `path` is relative, with no `..`.

## What the app guarantees

- Installing a version never touches other versions.
- Nothing is deleted unless the user removes it.
- Every file is checked against its `size` and `sha256` before it counts as installed.
- Interrupted downloads resume.

## Publishing, with the `voxa-catalogue` tool

```bash
cd app && cargo build -p voxa-models --bin voxa-catalogue
B=target/debug/voxa-catalogue
$B keygen ~/voxa-catalogue.key            # once; prints the public key for the app build
$B hash path/to/model.onnx                # sha256 and size for each file entry
$B sign catalogue.json ~/voxa-catalogue.key
$B verify catalogue.json <public-key-hex>
```

`sign` refuses to sign a catalogue the app would reject. Keep the secret key offline, never
in the repository. To rotate the key, ship an app build that trusts both the old and the new
key, then switch signing to the new one.
