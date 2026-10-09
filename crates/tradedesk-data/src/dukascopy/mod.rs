//! The Dukascopy cache reader.
//!
//! An implementation of the [`crate::Reader`] trait for the cache layout that
//! tradedesk-marketdata writes:
//! `<root>/<SYMBOL>/<YYYY>/<MM 00-indexed>/<DD>_<bid|ask>.csv.zst`.

pub mod error;
pub mod path_layout;
pub mod reader;

// FROZEN public surface — extended in a backwards-compatible way only.
pub use error::DukascopyError;
pub use path_layout::{DukascopyMonth, ParsedDayPath, PathParseError, day_csv_zst, parse_day_path};
pub use reader::DukascopyReader;
