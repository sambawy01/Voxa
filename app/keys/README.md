# Signing keys

- `dev-catalogue.key` / `dev-catalogue.pub`: the **development** key. The secret is committed
  on purpose, so anyone working on Voxa can sign test catalogues and voice packs. Only
  **debug builds** trust it (`cfg(debug_assertions)`); release builds never do.
- `production-catalogue.pub`: Voxa's production public key, once the owner generates it with
  `voxa-catalogue keygen` (see docs/CATALOGUE.md). Its secret is never in the repository.
  Release builds trust only this key.
