//! Plan 02-05 / CACHE-06 Arrow IPC schema snapshot + invariant gates.
//!
//! Three `insta` snapshots pin a structured human-readable rendering of the
//! [`build_arrow_schema`] output for three (timeframe, side) combos. Any drift
//! in field order, type, nullability, or metadata key set fails CI.
//!
//! ## Why not `assert_debug_snapshot!(schema)` directly?
//!
//! Two reasons:
//!
//! 1. `Schema::metadata()` returns a `HashMap`. Its `Debug` output iterates in
//!    hash-randomised order, so a raw Debug snapshot would be non-deterministic.
//!    The on-disk Arrow IPC bytes ARE deterministic because the IPC encoder
//!    sorts metadata internally
//!    (`arrow-ipc-58.3.0/src/convert.rs:136-137`), but the in-memory Debug
//!    form is not.
//! 2. The metadata includes `code_revision = <git SHA>`, which changes every
//!    commit; a raw snapshot would drift on every commit. The renderer redacts
//!    `code_revision` to a stable placeholder.
//!
//! The renderer below sorts metadata keys (so the snapshot is deterministic)
//! and redacts the `code_revision` value. The downstream Arrow IPC byte
//! determinism is gated by the
//! `cache_smoke::arrow_bytes_deterministic_under_shuffled_construction`
//! proptest.
//!
//! ## Non-snapshot invariant gates
//!
//! Three explicit `assert_eq!` tests pin the machine-readable invariants so
//! the failure mode is unambiguous (any snapshot diff is visually large; an
//! `assert_eq!` failure prints the precise drift):
//!
//! - `schema_metadata_keys_sorted` — `Schema::metadata().keys()` collected,
//!   sorted, matches the expected key set.
//! - `schema_field_order_locked` — exact field order.
//! - `schema_field_types_locked` — `tick_volume: Float64` (A1 invariant —
//!   never a `u32` tick-count column).

use arrow::datatypes::{DataType, Schema, TimeUnit};

use tradedesk_data::cache::build_arrow_schema;
use tradedesk_data::{Side, Timeframe};

// ===========================================================================
// Snapshot renderer
// ===========================================================================

/// Render `schema` to a stable, sorted text form for snapshot pinning.
///
/// Format (one section per line):
///
/// ```text
/// fields:
///   <name>: <DataType> nullable=<bool>
///   ...
/// metadata (sorted):
///   <key>: <value>
///   ...
/// ```
///
/// `code_revision` is redacted to `<redacted-code-revision>` so the snapshot
/// does not drift on every commit.
fn render_schema(schema: &Schema) -> String {
    use std::fmt::Write as _;
    let mut out = String::new();

    out.push_str("fields:\n");
    for f in schema.fields() {
        let _ = writeln!(
            out,
            "  {}: {:?} nullable={}",
            f.name(),
            f.data_type(),
            f.is_nullable()
        );
    }

    out.push_str("metadata (sorted):\n");
    let mut keys: Vec<&String> = schema.metadata().keys().collect();
    keys.sort();
    for k in keys {
        let v = schema.metadata().get(k).expect("key from keys()");
        let display_value: &str = if k == "code_revision" {
            "<redacted-code-revision>"
        } else {
            v.as_str()
        };
        let _ = writeln!(out, "  {k}: {display_value}");
    }

    out
}

// ===========================================================================
// Insta snapshots
// ===========================================================================

#[test]
fn arrow_schema_bytes_pinned_15m_bid() {
    let schema = build_arrow_schema("dukascopy", "EURUSD", Side::Bid, Timeframe::Tf15m);
    insta::assert_snapshot!(render_schema(&schema));
}

#[test]
fn arrow_schema_bytes_pinned_1h_ask() {
    let schema = build_arrow_schema("dukascopy", "GBPUSD", Side::Ask, Timeframe::Tf1h);
    insta::assert_snapshot!(render_schema(&schema));
}

#[test]
fn arrow_schema_bytes_pinned_1d_bid() {
    let schema = build_arrow_schema("dukascopy", "USDJPY", Side::Bid, Timeframe::Tf1d);
    insta::assert_snapshot!(render_schema(&schema));
}

// ===========================================================================
// Metadata-keys-sorted gate (B4 fix from Task 1 step 5(a))
// ===========================================================================

#[test]
fn schema_metadata_keys_sorted() {
    let schema = build_arrow_schema("dukascopy", "EURUSD", Side::Bid, Timeframe::Tf15m);
    let mut keys: Vec<&String> = schema.metadata().keys().collect();
    keys.sort();

    let expected = [
        "aggregator_version",
        "arrow_schema_version",
        "code_revision",
        "side",
        "source_id",
        "symbol",
        "timeframe",
    ];
    let sorted_strs: Vec<&str> = keys.iter().map(|s| s.as_str()).collect();
    assert_eq!(
        sorted_strs, expected,
        "schema metadata key set must match the expected list (sorted)"
    );

    for k in &expected {
        assert!(
            schema.metadata().contains_key(*k),
            "schema metadata missing key {k:?}"
        );
    }
}

// ===========================================================================
// Field-order gate
// ===========================================================================

#[test]
fn schema_field_order_locked() {
    let schema = build_arrow_schema("dukascopy", "EURUSD", Side::Bid, Timeframe::Tf15m);
    let names: Vec<&str> = schema.fields().iter().map(|f| f.name().as_str()).collect();
    assert_eq!(
        names,
        vec![
            "ts_open_utc",
            "ts_close_utc",
            "open",
            "high",
            "low",
            "close",
            "tick_volume",
        ],
        "Arrow schema field order is frozen by ARROW_SCHEMA_VERSION — any drift is a contract break"
    );
}

// ===========================================================================
// Field-type gate (A1 invariant)
// ===========================================================================

#[test]
fn schema_field_types_locked() {
    let schema = build_arrow_schema("dukascopy", "EURUSD", Side::Bid, Timeframe::Tf15m);

    assert_eq!(
        schema
            .field_with_name("tick_volume")
            .expect("tick_volume present")
            .data_type(),
        &DataType::Float64,
        "tick_volume must be Float64 — A1 invariant"
    );

    let ts_type = DataType::Timestamp(TimeUnit::Nanosecond, Some("UTC".into()));
    assert_eq!(
        schema
            .field_with_name("ts_open_utc")
            .expect("ts_open_utc present")
            .data_type(),
        &ts_type
    );
    assert_eq!(
        schema
            .field_with_name("ts_close_utc")
            .expect("ts_close_utc present")
            .data_type(),
        &ts_type
    );

    for fname in ["open", "high", "low", "close"] {
        assert_eq!(
            schema
                .field_with_name(fname)
                .unwrap_or_else(|_| panic!("{fname} present"))
                .data_type(),
            &DataType::Float64,
            "field {fname} must be Float64"
        );
    }

    // Nullability: every field is non-null.
    for f in schema.fields() {
        assert!(!f.is_nullable(), "field {} must be non-nullable", f.name());
    }
}
