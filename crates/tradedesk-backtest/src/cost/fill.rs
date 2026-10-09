//! Fill simulation: one venue's cost model applied to one joined bar.
//!
//! A fill is the configured price side's price, plus half the configured venue spread
//! for a buy or minus it for a sell, then adverse slippage (fixed points and basis
//! points), then commission. The spread is the one in force at the fill instant: a
//! spread with a time-of-day schedule ([`super::config::Spread::pips_at`]) charges the
//! window the fill falls in. The Dukascopy ask − bid on the bar is never the cost (the
//! venue-cost Decision on issue #28). It is only checked against the instrument's
//! `divergence_tolerance`, and a bar beyond the tolerance makes the fill an error.
//! There is no fallback to a narrower or zero spread.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use crate::JoinedBar;
use crate::Ohlcv;

use super::config::{InstrumentSpec, PriceSide, VenueInstrumentCost};

/// Buy or sell.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TradeSide {
    /// Pays the offer: reference price plus half the spread, plus slippage.
    Buy,
    /// Hits the bid: reference price minus half the spread, minus slippage.
    Sell,
}

impl TradeSide {
    /// `+1.0` for a buy, `-1.0` for a sell.
    #[must_use]
    pub fn sign(self) -> f64 {
        match self {
            Self::Buy => 1.0,
            Self::Sell => -1.0,
        }
    }
}

/// Where in a bar a fill happens.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "at", content = "raw")]
pub enum PricePoint {
    /// The bar's open on the configured side (a market order at the bar open).
    Open,
    /// The bar's close on the configured side (a market order at the bar close).
    Close,
    /// A price level inside the bar, in raw units on the configured side (a stop or a
    /// target).
    Level(f64),
}

/// Why a fill cannot be priced.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum FillError {
    /// The bar's ask − bid exceeds the instrument's tolerance: the bar is treated as
    /// corrupt rather than filled at a guessed price.
    #[error(
        "{symbol} at {ts}: |ask - bid| at the bar {point} is {divergence} pips/points, \
         beyond the {tolerance} tolerance"
    )]
    Divergence {
        /// Instrument.
        symbol: String,
        /// Bar open.
        ts: DateTime<Utc>,
        /// `"open"` or `"close"`.
        point: &'static str,
        /// The measured |ask − bid| in pips/points.
        divergence: f64,
        /// The configured tolerance.
        tolerance: f64,
    },
    /// Size must be finite and greater than zero.
    #[error("fill size {0} must be finite and greater than zero")]
    InvalidSize(f64),
    /// A price on the bar (or the level) is not finite.
    #[error("{symbol} at {ts}: a fill price input is not finite")]
    NonFinite {
        /// Instrument.
        symbol: String,
        /// Bar open.
        ts: DateTime<Utc>,
    },
}

/// A priced fill, before it is booked into a ledger.
///
/// Prices are in quote units. `spread_cost` and `slippage_cost` are money in the quote
/// currency for this fill's size. `commission` is money in the account currency and
/// covers only the per-fill parts; the per-round-trip commission is charged by the
/// ledger on the closing fill.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct FillQuote {
    /// Configured-side price before any cost, raw cache units.
    pub raw_price: f64,
    /// `raw_price` in quote units: the mark.
    pub mark_price: f64,
    /// The executed price: mark ± half spread ± slippage.
    pub fill_price: f64,
    /// Half the venue spread, in quote price.
    pub half_spread: f64,
    /// Adverse slippage, in quote price.
    pub slippage: f64,
    /// `half_spread × size`, quote currency.
    pub spread_cost: f64,
    /// `slippage × size`, quote currency.
    pub slippage_cost: f64,
    /// `per_fill + per_unit_per_fill × size`, account currency.
    pub commission: f64,
}

/// One venue's costs for one instrument.
#[derive(Debug, Clone, PartialEq)]
pub struct FillModel {
    symbol: String,
    spec: InstrumentSpec,
    cost: VenueInstrumentCost,
}

impl FillModel {
    /// A fill model for `symbol` from its spec and its venue costs.
    #[must_use]
    pub fn new(symbol: impl Into<String>, spec: InstrumentSpec, cost: VenueInstrumentCost) -> Self {
        Self {
            symbol: symbol.into(),
            spec,
            cost,
        }
    }

    /// Instrument symbol.
    #[must_use]
    pub fn symbol(&self) -> &str {
        &self.symbol
    }

    /// The instrument spec.
    #[must_use]
    pub fn spec(&self) -> &InstrumentSpec {
        &self.spec
    }

    /// The venue costs.
    #[must_use]
    pub fn cost(&self) -> &VenueInstrumentCost {
        &self.cost
    }

    /// The configured price side.
    #[must_use]
    pub fn price_side(&self) -> PriceSide {
        self.cost.price_side
    }

    /// The bar's OHLC on the configured side, in raw units. `Mid` averages bid and ask
    /// field by field, so its high and low are the averages of the two sides' extremes.
    #[must_use]
    pub fn reference(&self, bar: &JoinedBar) -> Ohlcv {
        match self.cost.price_side {
            PriceSide::Bid => bar.bid,
            PriceSide::Ask => bar.ask,
            PriceSide::Mid => Ohlcv {
                open: f64::midpoint(bar.bid.open, bar.ask.open),
                high: f64::midpoint(bar.bid.high, bar.ask.high),
                low: f64::midpoint(bar.bid.low, bar.ask.low),
                close: f64::midpoint(bar.bid.close, bar.ask.close),
                tick_volume: bar.bid.tick_volume + bar.ask.tick_volume,
            },
        }
    }

    /// The configured-side close of `bar`, in quote units. This is the mark used for
    /// mark-to-market and for financing notional.
    #[must_use]
    pub fn mark_close(&self, bar: &JoinedBar) -> f64 {
        self.spec.to_quote(self.reference(bar).close)
    }

    /// Check the bar's |ask − bid| at `point` against the tolerance. A `Level` fill
    /// checks both the open and the close, because the level lies between them.
    ///
    /// # Errors
    /// [`FillError::Divergence`] when a checked pair is beyond the tolerance, and
    /// [`FillError::NonFinite`] when a checked price is not finite.
    pub fn check_divergence(&self, bar: &JoinedBar, point: PricePoint) -> Result<(), FillError> {
        let pairs: &[(&'static str, f64, f64)] = match point {
            PricePoint::Open => &[("open", bar.bid.open, bar.ask.open)],
            PricePoint::Close => &[("close", bar.bid.close, bar.ask.close)],
            PricePoint::Level(_) => &[
                ("open", bar.bid.open, bar.ask.open),
                ("close", bar.bid.close, bar.ask.close),
            ],
        };
        for &(name, bid, ask) in pairs {
            if !bid.is_finite() || !ask.is_finite() {
                return Err(self.non_finite(bar));
            }
            let divergence = self.spec.raw_to_pips((ask - bid).abs());
            if divergence > self.spec.divergence_tolerance {
                return Err(FillError::Divergence {
                    symbol: self.symbol.clone(),
                    ts: bar.ts_open_utc,
                    point: name,
                    divergence,
                    tolerance: self.spec.divergence_tolerance,
                });
            }
        }
        Ok(())
    }

    /// Price a fill of `size` instrument units, buying or selling per `trade`, at `point` of
    /// `bar`. `at` is the fill instant: it selects the spread when the spread has a
    /// time-of-day schedule (a close fill happens at the bar's close, not its open).
    ///
    /// # Errors
    /// [`FillError::InvalidSize`] for a size that is not finite and positive,
    /// [`FillError::Divergence`] when the bar's sides diverge beyond the tolerance, and
    /// [`FillError::NonFinite`] when a price input is not finite.
    pub fn quote(
        &self,
        bar: &JoinedBar,
        point: PricePoint,
        at: DateTime<Utc>,
        trade: TradeSide,
        size: f64,
    ) -> Result<FillQuote, FillError> {
        if !size.is_finite() || size <= 0.0 {
            return Err(FillError::InvalidSize(size));
        }
        self.check_divergence(bar, point)?;
        let reference = self.reference(bar);
        let raw_price = match point {
            PricePoint::Open => reference.open,
            PricePoint::Close => reference.close,
            PricePoint::Level(level) => level,
        };
        if !raw_price.is_finite() {
            return Err(self.non_finite(bar));
        }
        Ok(self.price(raw_price, at, trade, size))
    }

    /// The cost arithmetic on an already-chosen raw reference price. No divergence check:
    /// callers go through [`FillModel::quote`].
    fn price(&self, raw_price: f64, at: DateTime<Utc>, trade: TradeSide, size: f64) -> FillQuote {
        let mark_price = self.spec.to_quote(raw_price);
        let half_spread = self.spec.pips_to_quote(self.cost.spread.pips_at(at)) / 2.0;
        let slippage = self.spec.pips_to_quote(self.cost.slippage.points)
            + mark_price.abs() * self.cost.slippage.bps / 10_000.0;
        let fill_price = mark_price + trade.sign() * (half_spread + slippage);
        let commission =
            self.cost.commission.per_fill + self.cost.commission.per_unit_per_fill * size;
        FillQuote {
            raw_price,
            mark_price,
            fill_price,
            half_spread,
            slippage,
            spread_cost: half_spread * size,
            slippage_cost: slippage * size,
            commission,
        }
    }

    fn non_finite(&self, bar: &JoinedBar) -> FillError {
        FillError::NonFinite {
            symbol: self.symbol.clone(),
            ts: bar.ts_open_utc,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cost::config::{Commission, CostConfig};
    use chrono::TimeZone;
    use proptest::prelude::*;

    fn side(open: f64, high: f64, low: f64, close: f64) -> Ohlcv {
        Ohlcv {
            open,
            high,
            low,
            close,
            tick_volume: 1.0,
        }
    }

    fn bar(bid: Ohlcv, ask: Ohlcv) -> JoinedBar {
        JoinedBar {
            ts_open_utc: T,
            bid,
            ask,
        }
    }

    /// The fill instant used where the time of day does not matter.
    const T: DateTime<Utc> = DateTime::from_timestamp(1_710_511_200, 0).unwrap(); // 2024-03-15 14:00Z

    fn utc(y: i32, mo: u32, d: u32, h: u32, mi: u32) -> DateTime<Utc> {
        Utc.with_ymd_and_hms(y, mo, d, h, mi, 0).unwrap()
    }

    fn model(venue: &str, symbol: &str) -> FillModel {
        let c = CostConfig::builtin();
        FillModel::new(
            symbol,
            c.instrument(symbol).unwrap().clone(),
            c.venue(venue).unwrap().instruments[symbol].clone(),
        )
    }

    fn with_cost(mut m: FillModel, f: impl FnOnce(&mut VenueInstrumentCost)) -> FillModel {
        f(&mut m.cost);
        m
    }

    /// `m` with a flat spread and fixed slippage, so the arithmetic is independent of the
    /// builtin figures.
    fn flat(m: FillModel, spread: f64, slippage: f64) -> FillModel {
        with_cost(m, |c| {
            c.spread.pips = spread;
            c.spread.schedule.clear();
            c.spread.tz = None;
            c.slippage.points = slippage;
        })
    }

    fn eurusd_bar() -> JoinedBar {
        // Dukascopy spread 0.3 pips at the close; the venue's 0.6 is what is charged.
        bar(
            side(10880.0, 10885.0, 10875.0, 10882.0),
            side(10880.2, 10885.3, 10875.2, 10882.3),
        )
    }

    fn close(a: f64, b: f64) -> bool {
        (a - b).abs() < 1e-12 * a.abs().max(1.0)
    }

    #[test]
    fn buy_and_sell_at_the_close_pay_half_the_spread_and_the_slippage() {
        let m = flat(model("ig_spread_bet", "EURUSD"), 0.6, 0.2);
        let b = eurusd_bar();
        let mid = f64::midpoint(1.0882, 1.088_23);
        // Half of a 0.6-pip spread plus 0.2 pips of slippage, each side.
        let buy = m
            .quote(&b, PricePoint::Close, T, TradeSide::Buy, 10_000.0)
            .unwrap();
        assert!(close(buy.mark_price, mid));
        assert!(close(buy.fill_price, mid + 0.000_03 + 0.000_02));
        assert!(close(buy.spread_cost, 0.000_03 * 10_000.0));
        assert!(close(buy.slippage_cost, 0.000_02 * 10_000.0));
        assert_eq!(buy.commission, 0.0);
        let sell = m
            .quote(&b, PricePoint::Close, T, TradeSide::Sell, 10_000.0)
            .unwrap();
        assert!(close(sell.fill_price, mid - 0.000_05));
    }

    #[test]
    fn the_dukascopy_spread_never_enters_the_fill() {
        let m = model("ig_spread_bet", "EURUSD");
        let narrow = eurusd_bar();
        // Same mid, Dukascopy spread 20x wider: the fill must not move.
        let wide = bar(
            side(10877.0, 10882.0, 10872.0, 10879.15),
            side(10883.2, 10888.3, 10878.2, 10885.15),
        );
        let a = m
            .quote(&narrow, PricePoint::Close, T, TradeSide::Buy, 1.0)
            .unwrap();
        let b = m
            .quote(&wide, PricePoint::Close, T, TradeSide::Buy, 1.0)
            .unwrap();
        assert!(close(a.fill_price, b.fill_price));
        assert!(close(a.half_spread, b.half_spread));
    }

    #[test]
    fn price_side_selects_bid_ask_or_mid() {
        let b = eurusd_bar();
        let base = with_cost(model("ig_spread_bet", "EURUSD"), |c| {
            c.spread.pips = 0.0;
            c.slippage.points = 0.0;
        });
        for (ps, expect) in [
            (PriceSide::Bid, 1.0882),
            (PriceSide::Ask, 1.088_23),
            (PriceSide::Mid, f64::midpoint(1.0882, 1.088_23)),
        ] {
            let m = with_cost(base.clone(), |c| c.price_side = ps);
            let q = m
                .quote(&b, PricePoint::Close, T, TradeSide::Buy, 1.0)
                .unwrap();
            assert!(close(q.fill_price, expect), "{ps:?}");
        }
    }

    #[test]
    fn open_and_level_points_use_their_own_reference() {
        let m = with_cost(model("ig_spread_bet", "EURUSD"), |c| {
            c.spread.pips = 0.0;
            c.slippage.points = 0.0;
            c.price_side = PriceSide::Bid;
        });
        let b = eurusd_bar();
        let open = m
            .quote(&b, PricePoint::Open, T, TradeSide::Sell, 1.0)
            .unwrap();
        assert!(close(open.fill_price, 1.088));
        let level = m
            .quote(&b, PricePoint::Level(10_878.5), T, TradeSide::Sell, 1.0)
            .unwrap();
        assert_eq!(level.raw_price, 10_878.5);
        assert!(close(level.fill_price, 1.087_85));
    }

    #[test]
    fn gold_and_index_spreads_are_in_their_own_points() {
        let gold = flat(model("ig_spread_bet", "XAUUSD"), 30.0, 5.0);
        let b = bar(
            side(206_300.0, 206_400.0, 206_200.0, 206_350.0),
            side(206_330.0, 206_430.0, 206_230.0, 206_380.0),
        );
        let q = gold
            .quote(&b, PricePoint::Close, T, TradeSide::Buy, 2.0)
            .unwrap();
        // $0.30 spread => $0.15 half; $0.05 slippage.
        assert!(close(q.mark_price, 2063.65));
        assert!(close(q.fill_price, 2063.65 + 0.15 + 0.05));
        assert!(close(q.spread_cost, 0.30));
        let spx = flat(model("ig_spread_bet", "USA500IDXUSD"), 0.4, 0.1);
        let b = bar(
            side(4774.0, 4775.0, 4773.0, 4774.2),
            side(4774.5, 4775.5, 4773.5, 4774.7),
        );
        let q = spx
            .quote(&b, PricePoint::Close, T, TradeSide::Sell, 1.0)
            .unwrap();
        assert!(close(q.fill_price, 4774.45 - 0.2 - 0.1));
    }

    #[test]
    fn razor_charges_commission_per_unit_and_bps_slippage_scales_with_price() {
        let razor = model("pepperstone_razor", "EURUSD");
        let q = razor
            .quote(
                &eurusd_bar(),
                PricePoint::Close,
                T,
                TradeSide::Buy,
                100_000.0,
            )
            .unwrap();
        // GBP 2.25 per 100,000 units per side.
        assert!(close(q.commission, 2.25));
        let m = with_cost(model("ig_spread_bet", "USA500IDXUSD"), |c| {
            c.spread.pips = 0.0;
            c.slippage.points = 0.0;
            c.slippage.bps = 2.0;
            c.commission = Commission {
                per_fill: 1.5,
                per_unit_per_fill: 0.25,
                per_round_trip: 9.0,
                source: "test".to_owned(),
                placeholder: true,
                excluded: false,
                reason: None,
            };
        });
        let b = bar(
            side(5000.0, 5000.0, 5000.0, 5000.0),
            side(5000.0, 5000.0, 5000.0, 5000.0),
        );
        let q = m
            .quote(&b, PricePoint::Close, T, TradeSide::Buy, 4.0)
            .unwrap();
        assert!(close(q.slippage, 1.0));
        assert!(close(q.fill_price, 5001.0));
        // Round trip is not a per-fill charge.
        assert!(close(q.commission, 1.5 + 0.25 * 4.0));
    }

    #[test]
    fn divergence_beyond_tolerance_is_an_error_never_a_fallback() {
        let m = model("ig_spread_bet", "EURUSD");
        // 150 pips apart at the close (tolerance 100); open is fine.
        let b = bar(
            side(10880.0, 10885.0, 10875.0, 10882.0),
            side(10880.2, 11035.0, 10875.2, 11032.0),
        );
        assert!(
            m.quote(&b, PricePoint::Open, T, TradeSide::Buy, 1.0)
                .is_ok()
        );
        let err = m
            .quote(&b, PricePoint::Close, T, TradeSide::Buy, 1.0)
            .unwrap_err();
        match err {
            FillError::Divergence {
                point,
                divergence,
                tolerance,
                ..
            } => {
                assert_eq!(point, "close");
                assert!((divergence - 150.0).abs() < 1e-9);
                assert_eq!(tolerance, 100.0);
            }
            other => panic!("unexpected {other:?}"),
        }
        // A level fill checks both ends of the bar.
        assert!(matches!(
            m.quote(&b, PricePoint::Level(10_881.0), T, TradeSide::Buy, 1.0),
            Err(FillError::Divergence { .. })
        ));
    }

    #[test]
    fn rejects_bad_sizes_and_non_finite_prices() {
        let m = model("ig_spread_bet", "EURUSD");
        for size in [0.0, -1.0, f64::NAN, f64::INFINITY] {
            assert!(matches!(
                m.quote(&eurusd_bar(), PricePoint::Close, T, TradeSide::Buy, size),
                Err(FillError::InvalidSize(_))
            ));
        }
        assert!(matches!(
            m.quote(
                &eurusd_bar(),
                PricePoint::Level(f64::NAN),
                T,
                TradeSide::Buy,
                1.0
            ),
            Err(FillError::NonFinite { .. })
        ));
    }

    #[test]
    fn every_builtin_venue_prices_every_instrument() {
        let c = CostConfig::builtin();
        for (venue_id, venue) in c.venues() {
            for (symbol, cost) in &venue.instruments {
                let spec = c.instrument(symbol).unwrap();
                let m = FillModel::new(symbol, spec.clone(), cost.clone());
                let raw = 1.0 / spec.raw_scale; // quote price 1.0
                let b = bar(side(raw, raw, raw, raw), side(raw, raw, raw, raw));
                let buy = m
                    .quote(&b, PricePoint::Close, T, TradeSide::Buy, 1.0)
                    .unwrap();
                let sell = m
                    .quote(&b, PricePoint::Close, T, TradeSide::Sell, 1.0)
                    .unwrap();
                // An excluded spread charges nothing (and is never costed).
                if cost.spread.excluded {
                    assert!(
                        close(buy.fill_price, sell.fill_price),
                        "{venue_id} {symbol}"
                    );
                } else {
                    assert!(buy.fill_price > sell.fill_price, "{venue_id} {symbol}");
                }
            }
        }
    }

    fn half_spread_at(m: &FillModel, at: DateTime<Utc>) -> f64 {
        let spec = m.spec().clone();
        let raw = 5000.0 / spec.raw_scale;
        let b = bar(side(raw, raw, raw, raw), side(raw, raw, raw, raw));
        let q = m
            .quote(&b, PricePoint::Close, at, TradeSide::Buy, 1.0)
            .unwrap();
        q.half_spread / spec.pip_size * 2.0
    }

    #[test]
    fn a_scheduled_spread_is_the_window_the_fill_instant_falls_in() {
        // IG US 500: 14:30-21:00 London 0.4, 22:00-23:00 1.5, otherwise 0.6.
        let spx = model("ig_spread_bet", "USA500IDXUSD");
        let pips = |at| half_spread_at(&spx, at);
        // Winter (GMT): London = UTC.
        assert!(close(pips(utc(2024, 1, 16, 14, 29)), 0.6));
        assert!(close(pips(utc(2024, 1, 16, 14, 30)), 0.4));
        assert!(close(pips(utc(2024, 1, 16, 20, 59)), 0.4));
        assert!(close(pips(utc(2024, 1, 16, 21, 0)), 0.6));
        assert!(close(pips(utc(2024, 1, 16, 22, 30)), 1.5));
        assert!(close(pips(utc(2024, 1, 16, 3, 0)), 0.6));
        // Summer (BST): the windows move with London, so 13:45Z is 14:45 BST.
        assert!(close(pips(utc(2024, 7, 16, 13, 45)), 0.4));
        assert!(close(pips(utc(2024, 1, 16, 13, 45)), 0.6));
        assert!(close(pips(utc(2024, 7, 16, 21, 30)), 1.5));
    }

    #[test]
    fn a_window_can_wrap_midnight() {
        // IG Germany 40: 21:00-00:15 London 5, 00:15-07:00 4.
        let dax = model("ig_spread_bet", "DEUIDXEUR");
        let pips = |at| half_spread_at(&dax, at);
        assert!(close(pips(utc(2024, 1, 16, 21, 0)), 5.0));
        assert!(close(pips(utc(2024, 1, 16, 23, 59)), 5.0));
        assert!(close(pips(utc(2024, 1, 17, 0, 14)), 5.0));
        assert!(close(pips(utc(2024, 1, 17, 0, 15)), 4.0));
        assert!(close(pips(utc(2024, 1, 17, 8, 0)), 1.4));
    }

    #[test]
    fn the_fill_instant_not_the_bar_open_selects_the_window() {
        // A 1h bar opening 20:00Z in January: its open is in the 0.4 window and its close
        // (21:00Z) is not.
        let spx = model("ig_spread_bet", "USA500IDXUSD");
        let raw = 5000.0;
        let mut b = bar(side(raw, raw, raw, raw), side(raw, raw, raw, raw));
        b.ts_open_utc = utc(2024, 1, 16, 20, 0);
        let at_open = spx
            .quote(&b, PricePoint::Open, b.ts_open_utc, TradeSide::Buy, 1.0)
            .unwrap();
        let at_close = spx
            .quote(
                &b,
                PricePoint::Close,
                utc(2024, 1, 16, 21, 0),
                TradeSide::Buy,
                1.0,
            )
            .unwrap();
        assert!(close(at_open.half_spread, 0.2));
        assert!(close(at_close.half_spread, 0.3));
        assert!(close(at_close.spread_cost, 0.3));
    }

    #[test]
    fn a_new_york_schedule_follows_new_york_daylight_saving() {
        // Pepperstone Razor US500: 09:30-16:00 New York 0.4, 17:00-18:00 1.5, else 0.6.
        let spx = model("pepperstone_razor", "USA500IDXUSD");
        let pips = |at| half_spread_at(&spx, at);
        assert!(close(pips(utc(2024, 1, 16, 14, 30)), 0.4)); // 09:30 EST
        assert!(close(pips(utc(2024, 7, 16, 13, 30)), 0.4)); // 09:30 EDT
        assert!(close(pips(utc(2024, 7, 16, 21, 30)), 1.5)); // 17:30 EDT
        assert!(close(pips(utc(2024, 1, 16, 21, 30)), 0.6)); // 16:30 EST
    }

    #[test]
    fn an_unscheduled_spread_is_the_same_at_every_hour() {
        let m = model("ig_spread_bet", "EURUSD");
        for h in 0..24 {
            assert!(close(half_spread_at(&m, utc(2024, 1, 16, h, 0)), 1.04));
        }
    }

    proptest! {
        #[test]
        fn buy_fill_at_or_above_mark_and_sell_at_or_below(
            bid in 1_000.0f64..20_000.0,
            gap in 0.0f64..50.0,
            spread in 0.0f64..10.0,
            slip in 0.0f64..5.0,
            bps in 0.0f64..10.0,
            size in 0.001f64..1e6,
            ps in prop_oneof![Just(PriceSide::Bid), Just(PriceSide::Ask), Just(PriceSide::Mid)],
        ) {
            let m = with_cost(model("ig_spread_bet", "EURUSD"), |c| {
                c.spread.pips = spread;
                c.slippage.points = slip;
                c.slippage.bps = bps;
                c.price_side = ps;
            });
            let b = bar(side(bid, bid, bid, bid), side(bid + gap, bid + gap, bid + gap, bid + gap));
            let buy = m.quote(&b, PricePoint::Close, T, TradeSide::Buy, size).unwrap();
            let sell = m.quote(&b, PricePoint::Close, T, TradeSide::Sell, size).unwrap();
            prop_assert!(buy.fill_price >= buy.mark_price);
            prop_assert!(sell.fill_price <= sell.mark_price);
            prop_assert_eq!(buy.mark_price, sell.mark_price);
            prop_assert!(buy.spread_cost >= 0.0 && buy.slippage_cost >= 0.0);
        }
    }
}
