//! Voxa's model distribution, from the plan in docs/NATIVE_APP_VOICES.md:
//!
//! - [`catalogue`]: the signed `catalogue.json` listing every model and voice, verified
//!   with Ed25519 against keys built into the app, with rollback protection.
//! - [`download`]: fetches model files from Voxa's mirror with resume and sha256 checks.
//! - [`pack`]: signed voice packs, for installing a model from a folder (offline transfer,
//!   or before the mirror exists), verified exactly like downloads.
//! - [`store`]: the on-disk model store. Versions install side by side and are **never
//!   deleted or replaced silently**; only an explicit remove deletes one.

pub mod catalogue;
pub mod download;
pub mod pack;
pub mod store;

pub use catalogue::{Catalogue, CatalogueError, ModelEntry, ModelFile, Tier, TrustedKeys};
pub use download::{DownloadError, Downloader};
pub use pack::{import_pack, read_pack, PackError};
pub use store::{InstalledModel, ModelStore, StoreError};
