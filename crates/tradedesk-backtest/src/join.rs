//! Join the per-side 1-minute streams into [`JoinedBar`]s.
//!
//! Policy for a minute present on one side only: it never becomes a joined bar, it is
//! never zero-filled or forward-filled, and it is never dropped silently. Each one is
//! reported as a [`OneSidedMinute`], and the loader's [`crate::OneSidedPolicy`] decides
//! whether such a report fails the load or travels with the series.

use chrono::{DateTime, Utc};
use tradedesk_data::{RawBar, Side};

use crate::{JoinedBar, Ohlcv};

/// A minute that one side has and the other does not.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct OneSidedMinute {
    /// Open of the minute.
    pub ts_open_utc: DateTime<Utc>,
    /// The side that has the minute; the opposite side is missing it.
    pub present: Side,
}

/// Output of [`join_sides`].
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Joined {
    /// Minutes present on both sides, ascending.
    pub bars: Vec<JoinedBar>,
    /// Minutes present on one side only, ascending.
    pub one_sided: Vec<OneSidedMinute>,
}

/// Why two per-side streams could not be joined.
#[derive(Debug, thiserror::Error)]
pub enum JoinError<E>
where
    E: std::error::Error + 'static,
{
    /// The reader failed while streaming one side.
    #[error("reader error on the {} side: {source}", side.as_str())]
    Reader {
        /// The side being read.
        side: Side,
        /// The reader's error.
        #[source]
        source: E,
    },
    /// A side's stream was not strictly ascending (a repeat or a step backwards), which
    /// breaks the `Reader` ordering contract the merge depends on.
    #[error("{} stream out of order: {next} after {prev}", side.as_str())]
    OutOfOrder {
        /// The offending side.
        side: Side,
        /// The previous minute on that side.
        prev: DateTime<Utc>,
        /// The minute that did not follow it.
        next: DateTime<Utc>,
    },
}

/// One side's stream, with the ordering check the merge relies on.
struct SideStream<I> {
    iter: I,
    side: Side,
    last: Option<DateTime<Utc>>,
}

impl<I, E> SideStream<I>
where
    I: Iterator<Item = Result<RawBar, E>>,
    E: std::error::Error + 'static,
{
    fn pull(&mut self) -> Result<Option<RawBar>, JoinError<E>> {
        let Some(item) = self.iter.next() else {
            return Ok(None);
        };
        let bar = item.map_err(|source| JoinError::Reader {
            side: self.side,
            source,
        })?;
        if let Some(prev) = self.last {
            if bar.ts_open_utc <= prev {
                return Err(JoinError::OutOfOrder {
                    side: self.side,
                    prev,
                    next: bar.ts_open_utc,
                });
            }
        }
        self.last = Some(bar.ts_open_utc);
        Ok(Some(bar))
    }
}

/// Merge the bid and ask streams on `ts_open_utc`.
///
/// Both inputs must be strictly ascending (the `Reader` contract). The merge streams:
/// it holds one pending bar per side, never a whole side.
///
/// # Errors
/// [`JoinError::Reader`] when either stream yields an error, and
/// [`JoinError::OutOfOrder`] when either stream repeats a minute or steps backwards.
pub fn join_sides<E, B, A>(bid: B, ask: A) -> Result<Joined, JoinError<E>>
where
    E: std::error::Error + 'static,
    B: IntoIterator<Item = Result<RawBar, E>>,
    A: IntoIterator<Item = Result<RawBar, E>>,
{
    let mut bid = SideStream {
        iter: bid.into_iter(),
        side: Side::Bid,
        last: None,
    };
    let mut ask = SideStream {
        iter: ask.into_iter(),
        side: Side::Ask,
        last: None,
    };
    let mut out = Joined::default();
    let mut b = bid.pull()?;
    let mut a = ask.pull()?;
    loop {
        match (b, a) {
            (None, None) => break,
            (Some(x), None) => {
                out.one_sided.push(one_sided(&x, Side::Bid));
                b = bid.pull()?;
            }
            (None, Some(y)) => {
                out.one_sided.push(one_sided(&y, Side::Ask));
                a = ask.pull()?;
            }
            (Some(x), Some(y)) => match x.ts_open_utc.cmp(&y.ts_open_utc) {
                std::cmp::Ordering::Equal => {
                    out.bars.push(JoinedBar {
                        ts_open_utc: x.ts_open_utc,
                        bid: Ohlcv::from_raw(&x),
                        ask: Ohlcv::from_raw(&y),
                    });
                    b = bid.pull()?;
                    a = ask.pull()?;
                }
                std::cmp::Ordering::Less => {
                    out.one_sided.push(one_sided(&x, Side::Bid));
                    b = bid.pull()?;
                }
                std::cmp::Ordering::Greater => {
                    out.one_sided.push(one_sided(&y, Side::Ask));
                    a = ask.pull()?;
                }
            },
        }
    }
    Ok(out)
}

fn one_sided(bar: &RawBar, present: Side) -> OneSidedMinute {
    OneSidedMinute {
        ts_open_utc: bar.ts_open_utc,
        present,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::{Duration, TimeZone};
    use proptest::prelude::*;
    use std::collections::BTreeSet;

    type Item = Result<RawBar, std::io::Error>;

    fn t0() -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2024, 1, 2, 0, 0, 0).unwrap()
    }

    fn raw(minute: i64, price: f64) -> RawBar {
        let ts = t0() + Duration::minutes(minute);
        RawBar {
            ts_open_utc: ts,
            ts_close_utc: ts + Duration::minutes(1),
            open: price,
            high: price + 1.0,
            low: price - 1.0,
            close: price + 0.5,
            tick_volume: 1.0,
        }
    }

    fn stream(minutes: &[i64], price: f64) -> Vec<Item> {
        minutes.iter().map(|&m| Ok(raw(m, price))).collect()
    }

    #[test]
    fn pairs_matching_minutes_and_reports_one_sided_ones() {
        let joined = join_sides(stream(&[0, 1, 3], 100.0), stream(&[1, 2, 3, 4], 101.0)).unwrap();
        let opens: Vec<_> = joined.bars.iter().map(|b| b.ts_open_utc).collect();
        assert_eq!(
            opens,
            vec![t0() + Duration::minutes(1), t0() + Duration::minutes(3)]
        );
        assert_eq!(joined.bars[0].bid.open, 100.0);
        assert_eq!(joined.bars[0].ask.open, 101.0);
        assert_eq!(
            joined.one_sided,
            vec![
                OneSidedMinute {
                    ts_open_utc: t0(),
                    present: Side::Bid
                },
                OneSidedMinute {
                    ts_open_utc: t0() + Duration::minutes(2),
                    present: Side::Ask
                },
                OneSidedMinute {
                    ts_open_utc: t0() + Duration::minutes(4),
                    present: Side::Ask
                },
            ]
        );
    }

    #[test]
    fn empty_inputs_join_to_nothing() {
        let joined = join_sides(Vec::<Item>::new(), Vec::<Item>::new()).unwrap();
        assert_eq!(joined, Joined::default());
    }

    #[test]
    fn rejects_a_repeated_minute() {
        let err = join_sides(stream(&[0, 1], 1.0), stream(&[0, 0], 1.0)).unwrap_err();
        assert!(matches!(
            err,
            JoinError::OutOfOrder {
                side: Side::Ask,
                ..
            }
        ));
    }

    #[test]
    fn rejects_a_step_backwards() {
        let err = join_sides(stream(&[2, 1], 1.0), stream(&[1, 2], 1.0)).unwrap_err();
        assert!(matches!(
            err,
            JoinError::OutOfOrder {
                side: Side::Bid,
                ..
            }
        ));
    }

    #[test]
    fn surfaces_a_reader_error_with_its_side() {
        let mut ask = stream(&[0], 1.0);
        ask.push(Err(std::io::Error::other("corrupt day")));
        let err = join_sides(stream(&[0, 1], 1.0), ask).unwrap_err();
        assert!(matches!(
            err,
            JoinError::Reader {
                side: Side::Ask,
                ..
            }
        ));
        assert!(err.to_string().contains("ask side"));
    }

    proptest! {
        /// Join invariants over arbitrary minute sets: output strictly ascending, every
        /// joined minute is on both sides, every one-sided minute is on exactly the side
        /// it names, and joined + one-sided covers the union exactly once.
        #[test]
        fn join_partitions_the_union_of_minutes(
            bid_set in proptest::collection::btree_set(0_i64..2_000, 0..300),
            ask_set in proptest::collection::btree_set(0_i64..2_000, 0..300),
        ) {
            let bid: Vec<i64> = bid_set.iter().copied().collect();
            let ask: Vec<i64> = ask_set.iter().copied().collect();
            let joined = join_sides(stream(&bid, 1.0), stream(&ask, 2.0)).unwrap();

            let minute = |ts: DateTime<Utc>| (ts - t0()).num_minutes();
            for pair in joined.bars.windows(2) {
                prop_assert!(pair[0].ts_open_utc < pair[1].ts_open_utc);
            }
            for pair in joined.one_sided.windows(2) {
                prop_assert!(pair[0].ts_open_utc < pair[1].ts_open_utc);
            }
            let both: BTreeSet<i64> = bid_set.intersection(&ask_set).copied().collect();
            let joined_minutes: BTreeSet<i64> =
                joined.bars.iter().map(|b| minute(b.ts_open_utc)).collect();
            prop_assert_eq!(&joined_minutes, &both);
            prop_assert_eq!(joined_minutes.len(), joined.bars.len());
            for bar in &joined.bars {
                prop_assert_eq!(bar.bid.open, 1.0);
                prop_assert_eq!(bar.ask.open, 2.0);
            }
            for m in &joined.one_sided {
                let k = minute(m.ts_open_utc);
                match m.present {
                    Side::Bid => prop_assert!(bid_set.contains(&k) && !ask_set.contains(&k)),
                    Side::Ask => prop_assert!(ask_set.contains(&k) && !bid_set.contains(&k)),
                }
            }
            let union = bid_set.union(&ask_set).count();
            prop_assert_eq!(joined.bars.len() + joined.one_sided.len(), union);
        }
    }
}
