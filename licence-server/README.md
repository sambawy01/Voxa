# Voxa licence server

Activations for copies of the Voxa app **sold directly** (Paddle). App Store and Google
Play copies don't use it; the stores handle those purchases. It is separate from the
website (`server.py`): its own process, its own SQLite database, its own signing key.

Rules, from [docs/NATIVE_APP_VOICES.md](../docs/NATIVE_APP_VOICES.md):

- one payment buys the whole app, with **two activations** per licence
- activation happens once online; the app then checks a **signed licence file offline,
  forever**, so it keeps working if this server is ever down or gone
- to move to a new machine: **Deactivate this device** in the app, or **Release all
  devices** (self-service, 3 times a year), or ask support (`admin-reset`)

## Running it

```bash
pip install cryptography
python licence_server.py keygen /secure/licence.key   # prints the PUBLIC key for app builds
python licence_server.py create /data/licences.db "order 1234"   # prints a new licence key once
python licence_server.py serve /data/licences.db /secure/licence.key 8090
```

Build the app with `VOXA_LICENCE_URL=https://licence.<your-domain>` and
`VOXA_LICENCE_PUBKEY=<public key>`. Without them (development builds, store builds) the app
never asks for activation.

## Privacy and security

- **Licence keys are never stored.** The database keeps only their sha256. The signed licence
  names the licence by a short prefix of that hash.
- **The device id is anonymous:** a salted hash of the OS machine id, never the raw id.
- Access logs are off, so keys never end up in logs.
- Keep the signing key secret. Anyone who has it can make licences. To rotate it, ship an
  app build that trusts both keys before switching.

## Endpoints

| Method | Path | Body | Result |
|---|---|---|---|
| POST | `/v1/activate` | `{key, device_id, device_name}` | `{licence: {payload, sig}}`, or 409 when both seats are taken |
| POST | `/v1/deactivate` | `{key, activation}` | `{ok}` |
| POST | `/v1/reset` | `{key}` | `{ok, resets_left}`, or 429 when this year's resets are used |
| GET | `/health` | | `{ok}` |

Tests: `python -m unittest discover -s licence-server/tests`. The Rust client is tested
against this server end to end with `cargo test -p voxa-app -- --ignored licence_e2e`. Both
sides check the shared `tests/licence_vector.json`.
