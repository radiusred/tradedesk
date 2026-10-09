//! Insta snapshot pinning the on-wire JSON shape of [`GapManifest`].
//!
//! Future drift in any of:
//! - the `#[serde(tag = "kind", rename_all = "snake_case")]` discriminant
//!   on `GapReason`,
//! - the variant snake-case wire form (`missing_source_file`,
//!   `corrupt_source_file`, `intra_day_gap`),
//! - field renames on `GapManifest` / `GapSpan` / `GapReason::*`,
//! - the sort order of `gaps`,
//!
//! ...fails the snapshot diff. The snapshot file lives at
//! `crates/miner-core/tests/snapshots/gap_manifest_snapshot__gap_manifest_json_shape_pinned.snap`
//! and is committed alongside this test.
//!
//! Acceptance flow: on first run `cargo insta accept` writes the `.snap`
//! file; CI subsequently treats any difference as a failure.

use chrono::{NaiveDate, TimeZone, Utc};
use tradedesk_data::Side;
use tradedesk_data::gap::TimeRange;
use tradedesk_data::gap::{GapManifest, GapReason, GapSpan};

#[test]
fn gap_manifest_json_shape_pinned() {
    let manifest = GapManifest {
        source_id: "dukascopy".into(),
        symbol: "EURUSD".into(),
        side: Side::Bid,
        queried_range: TimeRange {
            start_utc: Utc.with_ymd_and_hms(2024, 6, 10, 0, 0, 0).unwrap(),
            end_utc: Utc.with_ymd_and_hms(2024, 6, 15, 0, 0, 0).unwrap(),
        },
        gaps: vec![
            GapSpan {
                start_utc: Utc.with_ymd_and_hms(2024, 6, 11, 0, 0, 0).unwrap(),
                end_utc: Utc.with_ymd_and_hms(2024, 6, 12, 0, 0, 0).unwrap(),
                reason: GapReason::MissingSourceFile {
                    date: NaiveDate::from_ymd_opt(2024, 6, 11).unwrap(),
                },
            },
            GapSpan {
                start_utc: Utc.with_ymd_and_hms(2024, 6, 12, 0, 0, 0).unwrap(),
                end_utc: Utc.with_ymd_and_hms(2024, 6, 13, 0, 0, 0).unwrap(),
                reason: GapReason::CorruptSourceFile {
                    date: NaiveDate::from_ymd_opt(2024, 6, 12).unwrap(),
                    detail: "zero-byte file".into(),
                },
            },
            GapSpan {
                start_utc: Utc.with_ymd_and_hms(2024, 6, 13, 13, 45, 0).unwrap(),
                end_utc: Utc.with_ymd_and_hms(2024, 6, 13, 13, 46, 0).unwrap(),
                reason: GapReason::IntraDayGap {
                    affected_minutes: 1,
                },
            },
        ],
    };

    insta::assert_json_snapshot!(manifest);
}
