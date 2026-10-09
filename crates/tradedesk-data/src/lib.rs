//! The tradedesk market-data layer.
//!
//! - [`reader`]: the [`Reader`] trait a market-data source implements, and the bar,
//!   side and range types it yields.
//! - [`calendar`]: the trading calendar a reader declares.
//! - [`aggregator`]: 1-minute bars to 5m / 15m / 1h / 1d [`BarFrame`]s.
//! - [`gap`]: gap detection over a reader's coverage, and the [`GapManifest`].
//! - [`cache`]: the Arrow IPC cache of aggregated frames, with its fingerprint sidecar.
//! - [`dukascopy`]: the reader for the
//!   `<root>/<SYMBOL>/<YYYY>/<MM 00-indexed>/<DD>_<bid|ask>.csv.zst` cache layout.
//!
//! Cut from tradedesk-miner (`miner-core` and `miner-reader-dukascopy`); the scan engine,
//! findings and configuration stay with the miner.

// Test fixtures and golden-comparison assertions legitimately use patterns that
// clippy::pedantic flags. These allows scope to cfg(test) only; production code stays
// under the full pedantic bar.
#![cfg_attr(
    test,
    allow(
        clippy::float_cmp,
        clippy::cast_precision_loss,
        clippy::cast_possible_truncation,
        clippy::cast_possible_wrap,
        clippy::cast_sign_loss,
        clippy::cast_lossless,
        clippy::comparison_to_empty,
        clippy::useless_conversion,
        clippy::unnecessary_fallible_conversions,
        clippy::needless_range_loop,
        clippy::manual_memcpy,
        clippy::similar_names,
        clippy::many_single_char_names,
        clippy::doc_lazy_continuation,
        clippy::len_zero,
    )
)]

pub mod aggregator;
pub mod cache;
pub mod calendar;
pub mod dukascopy;
pub mod gap;
pub mod reader;

/// Git SHA of the source revision that produced this build; `dirty-<sha>` when the tree
/// had uncommitted changes; `"unknown"` when git was unavailable (e.g., tarball builds).
///
/// Written into the bar cache's fingerprint sidecar and, by the backtester, into every
/// trial record.
pub const CODE_REVISION: &str = env!("TRADEDESK_CODE_REVISION");

pub use calendar::Calendar;
pub use reader::{Blake3Hex, ClosedRangeUtc, RawBar, Reader, Side};

pub use aggregator::{
    AGGREGATOR_VERSION, AggParams, AggregateError, BarFrame, Timeframe, aggregate,
};

pub use gap::{GapDetector, GapManifest, GapReason, GapSpan, TimeRange};

pub use cache::{
    ARROW_SCHEMA_VERSION, BarCache, CacheError, FingerprintSidecar, build_arrow_schema,
};
