//! Plan 02-06 / Task 2 — `DukascopyReader` dyn-compat regression gate (CACHE-02 / T-02-20).
//!
//! Mirrors the inline `reader_trait_object_safe` test in
//! `crates/miner-core/src/reader.rs` (Plan 02-01), but lives in
//! `miner-reader-dukascopy/tests/` so the regression is gated from BOTH sides
//! of the trait/impl seam:
//!
//! - **`miner-core` side** (`reader::tests::reader_trait_object_safe`): proves
//!   the `Reader` trait *declaration* stays dyn-compatible even if no concrete
//!   impl is in scope.
//! - **`miner-reader-dukascopy` side** (this file): proves the concrete
//!   [`DukascopyReader`] can be coerced to `&dyn Reader<Error = DukascopyError>`
//!   AND to `Box<dyn Reader<Error = DukascopyError>>` — i.e. the IMPL stays
//!   dyn-compatible.
//!
//! Either side catching a regression alone is necessary but not sufficient — a
//! trait can stay object-safe while an impl picks up a non-dyn-safe bound, or
//! vice-versa. Catching the regression at both sites is the contract.
//!
//! Pure compile-time gate. No filesystem touches at runtime — the constructor
//! takes a `PathBuf` but does NOT canonicalise / open it.

use tradedesk_data::Reader;
use tradedesk_data::dukascopy::{DukascopyError, DukascopyReader};

/// Compile-time proof that `DukascopyReader` is dyn-compatible against the
/// `Reader<Error = DukascopyError>` shape. If a future refactor adds a
/// non-dyn-safe bound (e.g. `Self: Sized` on a method, an `impl Trait` return
/// without an explicit `Box<dyn …>`, an associated `type` without `Sized`
/// bounds, etc.), this test fails to compile.
///
/// Mirrors the canonical Phase 1 pattern
/// (`crates/miner-core/src/findings/sink.rs` lines 399-409 —
/// `FindingSink` object-safety gate).
#[test]
fn dukascopy_reader_is_dyn_compatible() {
    // Helper consumes its argument as a `&dyn Reader<Error = …>`. Calling
    // `accept_reader(&reader)` below forces the compiler to coerce
    // `&DukascopyReader` to that trait object — which is only possible when
    // the trait is dyn-safe AND the impl carries no dyn-incompatible bound.
    fn accept_reader(_r: &dyn Reader<Error = DukascopyError>) {}

    let cache_root = std::path::PathBuf::from("/tmp/nonexistent-test-cache-root");
    let reader = DukascopyReader::new(&cache_root);
    accept_reader(&reader);

    // `Box<dyn …>` coercion: stricter than `&dyn …` because it requires the
    // type to be sized AND moveable into the box AND coerce-able. This is the
    // shape Phase 3 workers will use when constructing per-rayon-task readers.
    let _boxed: Box<dyn Reader<Error = DukascopyError>> = Box::new(reader);

    // No assertions: success means the file compiled. Reaching this point at
    // runtime confirms construction + coercion succeeded without panic, but
    // the primary contract is the compile-time gate.
}
