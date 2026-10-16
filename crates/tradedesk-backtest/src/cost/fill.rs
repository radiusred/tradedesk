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
//!
//! **Volatility slippage and spread excess** (task #195). With
//! [`SlippageModel::Volatility`] the slippage is `κ × R_used` in quote price, `κ` the
//! `market`, `stop` or `limit` coefficient by [`FillKind`], and with a `spread_excess`
//! block the fill pays an extra half-spread. Both read the instrument's one-minute data
//! ([`MinuteCosts`], attached with [`FillModel::attach_minutes`]):
//!
//! - `R(m)`: the one-minute range of the mid (one side's range for a one-sided minute).
//! - **Execution minute** of a fill at instant `τ`: for a market fill (entries, signal
//!   and end-of-run exits, the liquidation valuation, and a close-only stop or target),
//!   the first minute opening in `[τ, τ + max_gap_minutes)`; for a level fill, the first
//!   minute of the engine bar whose range on the configured side reaches the level (the
//!   minute of nearest approach when none does); for a gap fill at the bar open, the
//!   engine bar's first minute.
//! - `floor(τ)`: the median `R` of the last `floor_minutes` minutes opening before `τ`.
//!   `R_used = max(R(execution minute), floor)`; with no execution minute the floor
//!   alone; with neither, [`FillError::NoVolatility`].
//! - `w = s(execution minute) / baseline`, `s` the minute's
//!   `max(ask.open − bid.open, ask.close − bid.close)` and the baseline its median at the
//!   same UTC minute of day over the last `baseline_weekdays` weekdays before `τ`'s date
//!   that have a two-sided minute there (looking back at most twice that many weekdays).
//!   The extra half-spread is `half_spread × (min(w, cap) − 1)⁺`, half the configured
//!   venue spread in force at `τ` times the excess ratio. The store's ask − bid is only
//!   that ratio; it is never the cost, so the Decision on issue #28 holds (Decision on
//!   issue #195: <https://github.com/radiusred/tradedesk/issues/195#issuecomment-6219124050>).
//!
//! Every such fill records the detail in its [`FillCostDetail`].

use std::sync::Arc;

use chrono::{DateTime, Datelike, Duration, NaiveDate, Utc, Weekday};
use serde::{Deserialize, Serialize};

use crate::exit::ExitReason;
use crate::{BarTimeframe, JoinedBar, Ohlcv};

use super::config::{InstrumentSpec, PriceSide, SlippageModel, VenueInstrumentCost};
use super::minutes::MinuteCosts;

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

/// What kind of order a fill executes, which selects the volatility slippage coefficient.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FillKind {
    /// A market order: entries, signal and end-of-run exits, the liquidation valuation.
    Market,
    /// A stop, gap fills included.
    Stop,
    /// A target (a limit order), gap fills included.
    Limit,
}

impl FillKind {
    /// The kind of a closing fill booked for `reason`.
    #[must_use]
    pub fn for_exit(reason: ExitReason) -> Self {
        match reason {
            ExitReason::Stop => Self::Stop,
            ExitReason::Target => Self::Limit,
            ExitReason::Signal | ExitReason::EndOfRun => Self::Market,
        }
    }
}

/// Why the spread ratio `w` was or was not computed for a fill.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SpreadRatioStatus {
    /// Computed, at or below the cap.
    Ok,
    /// Computed and above the cap: `cap` was charged.
    Capped,
    /// No execution minute: no extra.
    NoExecMinute,
    /// The execution minute is one-sided: no extra.
    OneSided,
    /// Fewer than `baseline_weekdays / 2` (rounded up) baseline values: no extra.
    BaselineMissing,
    /// The baseline is zero or below: no extra.
    BaselineNonpositive,
    /// No spread excess is configured, or it is excluded: no extra.
    Absent,
}

/// What a fill under the volatility slippage or the spread excess was priced from.
/// Prices are quote units; `spread_excess_cost` is quote currency. A field that does not
/// apply is `None` and left out of the JSON, except `spread_baseline`, which is `null`.
/// With the one-minute bars, the record holds every input of the fill's slippage and
/// spread-excess charge.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct FillCostDetail {
    /// Market, stop or limit.
    pub fill_kind: FillKind,
    /// The slippage model.
    pub slippage_model: SlippageModel,
    /// The execution minute's open.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub exec_minute: Option<DateTime<Utc>>,
    /// `R` of the execution minute (volatility slippage only).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub range_exec: Option<f64>,
    /// The floor, the median `R` before the fill (volatility slippage only).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub range_floor: Option<f64>,
    /// `max(range_exec, range_floor)` (volatility slippage only).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub range_used: Option<f64>,
    /// The coefficient applied (volatility slippage only).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kappa: Option<f64>,
    /// The slippage charged, quote price.
    pub slippage: f64,
    /// `w`, when computed.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub spread_ratio: Option<f64>,
    /// The baseline spread `w` divides by, quote price; `null` when no baseline exists
    /// (no execution minute, a one-sided minute, too few baseline values, or no spread
    /// excess).
    #[serde(default)]
    pub spread_baseline: Option<f64>,
    /// Why `w` was or was not computed.
    pub spread_ratio_status: SpreadRatioStatus,
    /// The configured venue half-spread in force at `τ`, quote price.
    pub half_spread: f64,
    /// `half_spread × (min(w, cap) − 1)⁺`, quote price.
    pub extra_half_spread: f64,
    /// `extra_half_spread × size`, quote currency.
    pub spread_excess_cost: f64,
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
    /// A volatility slippage found neither an execution minute nor any earlier minute.
    #[error("{symbol} at {at}: no one-minute bar to take a volatility from")]
    NoVolatility {
        /// Instrument.
        symbol: String,
        /// The fill instant.
        at: DateTime<Utc>,
    },
    /// The cost needs one-minute data and none is attached to the fill model.
    #[error(
        "{symbol}: the volatility slippage or spread excess needs one-minute cost data, \
         and none is attached"
    )]
    NoMinuteData {
        /// Instrument.
        symbol: String,
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
    /// The executed price: mark ± (half spread + extra half-spread + slippage).
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
    /// The spread excess's extra half-spread, quote price (0 without one).
    pub extra_half_spread: f64,
    /// `extra_half_spread × size`, quote currency.
    pub spread_excess_cost: f64,
    /// What the volatility slippage and the spread excess were priced from; `None` when
    /// the cost uses neither (a fixed slippage and no spread excess).
    pub detail: Option<FillCostDetail>,
}

/// One instrument's one-minute data and the engine bar length its level and gap fills
/// scan.
#[derive(Debug, Clone, PartialEq)]
struct Attached {
    minutes: Arc<MinuteCosts>,
    engine_bar: Duration,
}

/// One venue's costs for one instrument.
#[derive(Debug, Clone, PartialEq)]
pub struct FillModel {
    symbol: String,
    spec: InstrumentSpec,
    cost: VenueInstrumentCost,
    attached: Option<Attached>,
}

impl FillModel {
    /// A fill model for `symbol` from its spec and its venue costs.
    #[must_use]
    pub fn new(symbol: impl Into<String>, spec: InstrumentSpec, cost: VenueInstrumentCost) -> Self {
        Self {
            symbol: symbol.into(),
            spec,
            cost,
            attached: None,
        }
    }

    /// Attach the instrument's one-minute data, which a volatility slippage or a spread
    /// excess reads, and the timeframe of the bars the engine fills on (a level or gap
    /// fill scans the minutes of that bar).
    pub fn attach_minutes(&mut self, minutes: Arc<MinuteCosts>, engine: BarTimeframe) {
        self.attached = Some(Attached {
            minutes,
            engine_bar: engine.duration(),
        });
    }

    /// The attached one-minute data, if any.
    #[must_use]
    pub fn minutes(&self) -> Option<&MinuteCosts> {
        self.attached.as_ref().map(|a| a.minutes.as_ref())
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

    /// Price a market fill of `size` instrument units, buying or selling per `trade`, at
    /// `point` of `bar`. `at` is the fill instant: it selects the spread when the spread
    /// has a time-of-day schedule (a close fill happens at the bar's close, not its open).
    /// [`FillModel::quote_as`] with [`FillKind::Market`].
    ///
    /// # Errors
    /// As [`FillModel::quote_as`].
    pub fn quote(
        &self,
        bar: &JoinedBar,
        point: PricePoint,
        at: DateTime<Utc>,
        trade: TradeSide,
        size: f64,
    ) -> Result<FillQuote, FillError> {
        self.quote_as(bar, point, at, trade, size, FillKind::Market)
    }

    /// Price a fill of `kind`, as [`FillModel::quote`]. The kind selects the volatility
    /// slippage coefficient and how the execution minute is found (see the module docs).
    ///
    /// # Errors
    /// [`FillError::InvalidSize`] for a size that is not finite and positive,
    /// [`FillError::Divergence`] when the bar's sides diverge beyond the tolerance,
    /// [`FillError::NonFinite`] when a price input is not finite,
    /// [`FillError::NoMinuteData`] when the cost needs one-minute data and none is
    /// attached, and [`FillError::NoVolatility`] when a volatility slippage has no minute
    /// to read.
    pub fn quote_as(
        &self,
        bar: &JoinedBar,
        point: PricePoint,
        at: DateTime<Utc>,
        trade: TradeSide,
        size: f64,
        kind: FillKind,
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
        let mut q = self.price(raw_price, at, trade, size);
        if self.cost.uses_extended_models() {
            let detail = self.detail(bar, point, at, kind, &q, size)?;
            q.slippage = detail.slippage;
            q.slippage_cost = detail.slippage * size;
            q.extra_half_spread = detail.extra_half_spread;
            q.spread_excess_cost = detail.spread_excess_cost;
            q.fill_price = q.mark_price
                + trade.sign() * (q.half_spread + detail.extra_half_spread + detail.slippage);
            q.detail = Some(detail);
        }
        Ok(q)
    }

    /// The cost arithmetic on an already-chosen raw reference price, under the fixed
    /// slippage. No divergence check: callers go through [`FillModel::quote_as`].
    fn price(&self, raw_price: f64, at: DateTime<Utc>, trade: TradeSide, size: f64) -> FillQuote {
        let mark_price = self.spec.to_quote(raw_price);
        let half_spread = self.spec.pips_to_quote(self.cost.spread.pips_at(at)) / 2.0;
        let slippage = self.fixed_slippage(mark_price);
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
            extra_half_spread: 0.0,
            spread_excess_cost: 0.0,
            detail: None,
        }
    }

    fn fixed_slippage(&self, mark_price: f64) -> f64 {
        self.spec.pips_to_quote(self.cost.slippage.points)
            + mark_price.abs() * self.cost.slippage.bps / 10_000.0
    }

    /// The volatility slippage and the spread excess of one fill (see the module docs).
    fn detail(
        &self,
        bar: &JoinedBar,
        point: PricePoint,
        at: DateTime<Utc>,
        kind: FillKind,
        fixed: &FillQuote,
        size: f64,
    ) -> Result<FillCostDetail, FillError> {
        let slip = &self.cost.slippage;
        let volatility = slip.model == SlippageModel::Volatility && !slip.excluded;
        let excess = self.cost.spread_excess.as_ref().filter(|x| !x.excluded);
        let mut d = FillCostDetail {
            fill_kind: kind,
            slippage_model: slip.model,
            exec_minute: None,
            range_exec: None,
            range_floor: None,
            range_used: None,
            kappa: None,
            slippage: if slip.model == SlippageModel::Fixed {
                fixed.slippage
            } else {
                0.0
            },
            spread_ratio: None,
            spread_baseline: None,
            spread_ratio_status: SpreadRatioStatus::Absent,
            half_spread: fixed.half_spread,
            extra_half_spread: 0.0,
            spread_excess_cost: 0.0,
        };
        if !volatility && excess.is_none() {
            return Ok(d);
        }
        let attached = self
            .attached
            .as_ref()
            .ok_or_else(|| FillError::NoMinuteData {
                symbol: self.symbol.clone(),
            })?;
        let minutes = attached.minutes.as_ref();
        let exec = self.exec_minute(minutes, attached.engine_bar, bar, point, at, kind);
        d.exec_minute = exec.map(|i| minutes.ts(i));
        if volatility {
            let range_exec = exec.map(|i| self.spec.to_quote(minutes.range(i)));
            let before = minutes.before(at);
            let n = usize::try_from(slip.floor_minutes()).unwrap_or(usize::MAX);
            let floor = median(
                (before.saturating_sub(n)..before)
                    .map(|i| minutes.range(i))
                    .collect(),
            )
            .map(|r| self.spec.to_quote(r));
            let range_used = match (range_exec, floor) {
                (Some(e), Some(f)) => e.max(f),
                (Some(r), None) | (None, Some(r)) => r,
                (None, None) => {
                    return Err(FillError::NoVolatility {
                        symbol: self.symbol.clone(),
                        at,
                    });
                }
            };
            let kappa = match kind {
                FillKind::Market => slip.market,
                FillKind::Stop => slip.stop,
                FillKind::Limit => slip.limit,
            }
            .unwrap_or_default();
            d.range_exec = range_exec;
            d.range_floor = floor;
            d.range_used = Some(range_used);
            d.kappa = Some(kappa);
            d.slippage = (kappa * range_used).max(0.0);
        }
        if let Some(x) = excess {
            let cap = x.cap;
            let (ratio, baseline, status) = spread_ratio(minutes, exec, at, x.baseline_weekdays());
            d.spread_ratio = ratio;
            d.spread_baseline = baseline.map(|b| self.spec.to_quote(b));
            d.spread_ratio_status = match (ratio, status) {
                (Some(w), _) if w > cap => SpreadRatioStatus::Capped,
                (_, status) => status,
            };
            if let Some(w) = ratio {
                d.extra_half_spread = fixed.half_spread * (w.min(cap) - 1.0).max(0.0);
                d.spread_excess_cost = d.extra_half_spread * size;
            }
        }
        Ok(d)
    }

    /// The execution minute's index (see the module docs), or `None` when there is none.
    fn exec_minute(
        &self,
        minutes: &MinuteCosts,
        engine_bar: Duration,
        bar: &JoinedBar,
        point: PricePoint,
        at: DateTime<Utc>,
        kind: FillKind,
    ) -> Option<usize> {
        let open = bar.ts_open_utc;
        let in_bar = minutes.before(open)..minutes.before(open + engine_bar);
        match (point, kind) {
            (PricePoint::Level(level), _) => {
                let side = self.cost.price_side;
                let below = level < self.reference(bar).open;
                let above = level > self.reference(bar).open;
                let reaches = |i: &usize| {
                    let (high, low) = minutes.high_low(*i, side);
                    (!below || low <= level) && (!above || high >= level)
                };
                in_bar.clone().find(reaches).or_else(|| {
                    // No minute reaches it (the bar's mid extreme can lie beyond every
                    // minute's): the minute of nearest approach, the earliest on a tie.
                    let distance = |i: usize| {
                        let (high, low) = minutes.high_low(i, side);
                        if below { low - level } else { level - high }
                    };
                    in_bar.fold(None, |best: Option<usize>, i| match best {
                        Some(b) if distance(b) <= distance(i) => Some(b),
                        _ => Some(i),
                    })
                })
            }
            (PricePoint::Open, FillKind::Stop | FillKind::Limit) => {
                (!in_bar.is_empty()).then_some(in_bar.start)
            }
            _ => {
                let gap = Duration::minutes(i64::from(self.cost.slippage.max_gap_minutes()));
                let i = minutes.before(at);
                (i < minutes.len() && minutes.ts(i) < at + gap).then_some(i)
            }
        }
    }

    fn non_finite(&self, bar: &JoinedBar) -> FillError {
        FillError::NonFinite {
            symbol: self.symbol.clone(),
            ts: bar.ts_open_utc,
        }
    }
}

/// The median of `values`; `None` when empty.
fn median(mut values: Vec<f64>) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    values.sort_by(f64::total_cmp);
    let mid = values.len() / 2;
    Some(if values.len() % 2 == 0 {
        f64::midpoint(values[mid - 1], values[mid])
    } else {
        values[mid]
    })
}

/// `w`, the baseline (raw units) and the status for a fill at `at` with execution
/// minute `exec`.
fn spread_ratio(
    minutes: &MinuteCosts,
    exec: Option<usize>,
    at: DateTime<Utc>,
    baseline_weekdays: u32,
) -> (Option<f64>, Option<f64>, SpreadRatioStatus) {
    let Some(i) = exec else {
        return (None, None, SpreadRatioStatus::NoExecMinute);
    };
    let Some(s) = minutes.spread(i) else {
        return (None, None, SpreadRatioStatus::OneSided);
    };
    let time = minutes.ts(i).time();
    let mut values = Vec::new();
    let mut day: NaiveDate = at.date_naive();
    let mut weekdays = 0;
    while weekdays < 2 * baseline_weekdays && values.len() < baseline_weekdays as usize {
        let Some(previous) = day.pred_opt() else {
            break;
        };
        day = previous;
        if matches!(day.weekday(), Weekday::Sat | Weekday::Sun) {
            continue;
        }
        weekdays += 1;
        if let Some(j) = minutes.find(day.and_time(time).and_utc()) {
            if let Some(v) = minutes.spread(j) {
                values.push(v);
            }
        }
    }
    if values.len() < baseline_weekdays.div_ceil(2) as usize {
        return (None, None, SpreadRatioStatus::BaselineMissing);
    }
    match median(values) {
        Some(baseline) if baseline > 0.0 => {
            (Some(s / baseline), Some(baseline), SpreadRatioStatus::Ok)
        }
        baseline => (None, baseline, SpreadRatioStatus::BaselineNonpositive),
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
        let m = flat(model("example_spread_bet", "EURUSD"), 0.6, 0.2);
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
        let m = model("example_spread_bet", "EURUSD");
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
        let base = with_cost(model("example_spread_bet", "EURUSD"), |c| {
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
        let m = with_cost(model("example_spread_bet", "EURUSD"), |c| {
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
        let gold = flat(model("example_spread_bet", "XAUUSD"), 30.0, 5.0);
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
        let spx = flat(model("example_spread_bet", "USA500IDXUSD"), 0.4, 0.1);
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
    fn a_cfd_charges_commission_per_unit_and_bps_slippage_scales_with_price() {
        let cfd = model("example_ny_cfd", "EURUSD");
        let q = cfd
            .quote(
                &eurusd_bar(),
                PricePoint::Close,
                T,
                TradeSide::Buy,
                100_000.0,
            )
            .unwrap();
        // 3.00 per 100,000 units per side.
        assert!(close(q.commission, 3.0));
        let m = with_cost(model("example_spread_bet", "USA500IDXUSD"), |c| {
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
        let m = model("example_spread_bet", "EURUSD");
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
        let m = model("example_spread_bet", "EURUSD");
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
        // Example US 500: 14:30-21:00 London 0.5, 21:00-23:00 2.0, otherwise 1.0.
        let spx = model("example_spread_bet", "USA500IDXUSD");
        let pips = |at| half_spread_at(&spx, at);
        // Winter (GMT): London = UTC.
        assert!(close(pips(utc(2024, 1, 16, 14, 29)), 1.0));
        assert!(close(pips(utc(2024, 1, 16, 14, 30)), 0.5));
        assert!(close(pips(utc(2024, 1, 16, 20, 59)), 0.5));
        assert!(close(pips(utc(2024, 1, 16, 21, 0)), 2.0));
        assert!(close(pips(utc(2024, 1, 16, 23, 0)), 1.0));
        assert!(close(pips(utc(2024, 1, 16, 3, 0)), 1.0));
        // Summer (BST): the windows move with London, so 13:45Z is 14:45 BST.
        assert!(close(pips(utc(2024, 7, 16, 13, 45)), 0.5));
        assert!(close(pips(utc(2024, 1, 16, 13, 45)), 1.0));
        assert!(close(pips(utc(2024, 7, 16, 21, 30)), 2.0));
        assert!(close(pips(utc(2024, 1, 16, 22, 30)), 2.0));
    }

    #[test]
    fn a_window_can_wrap_midnight() {
        // Example Germany 40: 22:00-01:00 London 6, 01:00-08:00 3.
        let dax = model("example_spread_bet", "DEUIDXEUR");
        let pips = |at| half_spread_at(&dax, at);
        assert!(close(pips(utc(2024, 1, 16, 22, 0)), 6.0));
        assert!(close(pips(utc(2024, 1, 16, 23, 59)), 6.0));
        assert!(close(pips(utc(2024, 1, 17, 0, 59)), 6.0));
        assert!(close(pips(utc(2024, 1, 17, 1, 0)), 3.0));
        assert!(close(pips(utc(2024, 1, 17, 8, 0)), 1.5));
    }

    #[test]
    fn the_fill_instant_not_the_bar_open_selects_the_window() {
        // A 1h bar opening 20:00Z in January: its open is in the 0.5 window and its close
        // (21:00Z) is in the 2.0 window.
        let spx = model("example_spread_bet", "USA500IDXUSD");
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
        assert!(close(at_open.half_spread, 0.25));
        assert!(close(at_close.half_spread, 1.0));
        assert!(close(at_close.spread_cost, 1.0));
    }

    #[test]
    fn a_new_york_schedule_follows_new_york_daylight_saving() {
        // Example CFD US500: 09:30-16:00 New York 0.5, 17:00-18:00 2.0, else 1.0.
        let spx = model("example_ny_cfd", "USA500IDXUSD");
        let pips = |at| half_spread_at(&spx, at);
        assert!(close(pips(utc(2024, 1, 16, 14, 30)), 0.5)); // 09:30 EST
        assert!(close(pips(utc(2024, 7, 16, 13, 30)), 0.5)); // 09:30 EDT
        assert!(close(pips(utc(2024, 7, 16, 21, 30)), 2.0)); // 17:30 EDT
        assert!(close(pips(utc(2024, 1, 16, 21, 30)), 1.0)); // 16:30 EST
    }

    #[test]
    fn an_unscheduled_spread_is_the_same_at_every_hour() {
        let m = model("example_spread_bet", "EURUSD");
        for h in 0..24 {
            assert!(close(half_spread_at(&m, utc(2024, 1, 16, h, 0)), 1.0));
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
            let m = with_cost(model("example_spread_bet", "EURUSD"), |c| {
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
