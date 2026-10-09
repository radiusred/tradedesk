//! The single-run engine: one strategy on one instrument at one venue, from loaded bars
//! to [`Metrics`].
//!
//! [`run`] steps the strategy's primary series (with its context series, through
//! [`BarFeed`]) and books what it asks for in a [`Ledger`], in the per-bar order
//! [`resolve_bar`] fixes:
//!
//! 1. with a position open, [`Ledger::check_exit`] evaluates the standing stop and target
//!    on the bar (they trade during it);
//! 2. the strategy sees the bar ([`BarFeed::step`], which calls `on_bar`);
//! 3. [`resolve_bar`] decides, and the engine books it:
//!    - an entry fills at the signal bar's close, sized by the [`SizingPolicy`], and
//!      [`FillBridge::anchor`] sends the raw-unit fill back to the strategy and the
//!      shifted stop and target to [`Ledger::set_levels`];
//!    - a ledger stop or target closes at its trigger point, and the strategy is told
//!      [`PositionFeedback::ClosedExternally`] after its `on_bar` unless it exited itself;
//!    - a strategy exit closes at the bar close through [`Ledger::close_signal`], which
//!      keeps the strategy's label;
//!    - a stop move goes to [`Ledger::set_levels`];
//! 4. with a position still open, [`Ledger::accrue_financing`] charges every rollover
//!    up to the bar close, marked from the instrument's 1-minute series.
//!
//! **Fill timestamps.** An entry, a strategy exit and a close-only stop fill at the bar
//! close and are stamped with it. A gap through a level fills at the open and is stamped
//! with the bar open. An intrabar level fill is stamped with the bar close: the moment
//! inside the bar is unknown, and the close is the conservative choice, because any
//! rollover inside the bar is charged.
//!
//! **Warm-up.** Primary bars that open before the metrics window's first day are shown
//! to the strategy so its indicators are ready, but nothing trades on them. An entry
//! signalled there is answered with [`PositionFeedback::EntryRejected`], and the strategy
//! restarts its cooldown as if the entry had filled and exited on that bar, so it does
//! not re-signal on every warm-up bar. Sizing refusals and divergent bars are not
//! rejections: they fail the run. Bars opening at or after the window's end are not
//! stepped.
//!
//! **End of run.** [`EndOfRun::Close`] closes a position still open after the last bar
//! at that bar's close, booked as [`ExitReason::EndOfRun`]. [`EndOfRun::LeaveOpen`]
//! leaves it to [`Metrics`], which values it at liquidation under `open_at_end`.
//!
//! **An entry on the final bar** ([`FinalBarEntry::FilledThenEndOfRun`]) is filled at
//! that bar's close like any other, entry cost charged, and is then an ordinary
//! position open after the last bar: under [`EndOfRun::Close`] it is booked at the same
//! close as [`ExitReason::EndOfRun`] with its full exit cost (a round trip held for zero
//! bars); under [`EndOfRun::LeaveOpen`] it is carried in `open_at_end`. Either way the
//! curve's last point reconciles with the trades and the open positions. The rule is
//! written into the ledger's [`RunMetadata`](crate::RunMetadata) (`end_of_run`), and
//! [`EngineStats::final_bar_entries`] counts such entries.
//!
//! **Errors.** Nothing falls back. A ledger refusal (a divergent bar, no mark), a strategy
//! error, a bridge out of step or an unusable size fails the run with [`EngineError`];
//! in a sweep that is the cell's recorded failure.
//!
//! The loop holds no clock and no randomness, so a run is deterministic for a given
//! (config, data).

use chrono::{DateTime, NaiveTime, Utc};
use serde::{Deserialize, Serialize};

use crate::cost::fill::{FillModel, PricePoint};
use crate::exit::{ExitEvaluation, ExitReason};
use crate::ledger::{AccountFx, FillAt, Ledger, LedgerError, PositionId};
pub use crate::ledger::{EndOfRun, EndOfRunRule, FinalBarEntry};
use crate::metrics::{Metrics, MetricsConfig, MetricsError};
use crate::strategy::{
    BarAction, BarFeed, BridgeError, Entry, FillBridge, PositionFeedback, RunError, Strategy,
    StrategyError, atr_normalised_size, resolve_bar,
};
use crate::{BarTimeframe, CostConfig, JoinedBar, MarketData};

/// How an entry is sized, in instrument units. Recorded with every trial.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum SizingPolicy {
    /// A fixed spread-bet stake in the account currency per pip/point:
    /// [`AccountFx::units_for_stake_per_point`].
    StakePerPoint {
        /// Account currency per pip/point (for example £1 a point).
        stake: f64,
    },
    /// ATR-normalised risk ([`atr_normalised_size`]): units such that a move of
    /// `atr_multiple` × the entry's ATR costs `risk` in the account currency, clamped to
    /// `[min_units, max_units]`. The point value is the account-currency P&L of one unit
    /// over one raw price unit, `raw_scale × quote→account rate`.
    AtrRisk {
        /// Account currency risked per trade.
        risk: f64,
        /// ATR multiple of the sizing stop.
        atr_multiple: f64,
        /// Lower bound, units.
        min_units: f64,
        /// Upper bound, units.
        max_units: f64,
    },
}

impl SizingPolicy {
    /// Units for `entry` on the instrument `model` prices, under `fx`.
    ///
    /// # Errors
    /// [`EngineError::Sizing`] when the policy's figures or the result are not finite
    /// and positive; [`EngineError::Ledger`] when `fx` has no rate for the quote currency.
    pub fn units(
        &self,
        entry: &Entry,
        model: &FillModel,
        fx: &AccountFx,
    ) -> Result<f64, EngineError> {
        let units = match *self {
            Self::StakePerPoint { stake } => {
                if !(stake.is_finite() && stake > 0.0) {
                    return Err(EngineError::Sizing("stake must be finite and > 0"));
                }
                fx.units_for_stake_per_point(model.spec(), stake)?
            }
            Self::AtrRisk {
                risk,
                atr_multiple,
                min_units,
                max_units,
            } => {
                let ok = |x: f64| x.is_finite() && x >= 0.0;
                if !(ok(risk) && ok(atr_multiple) && ok(min_units) && ok(max_units))
                    || max_units <= 0.0
                    || min_units > max_units
                {
                    return Err(EngineError::Sizing(
                        "risk, atr_multiple and the unit bounds must be finite, >= 0 and \
                         ordered, with max_units > 0",
                    ));
                }
                let point_value = model.spec().raw_scale * fx.rate(&model.spec().quote_currency)?;
                atr_normalised_size(
                    risk,
                    entry.atr,
                    atr_multiple,
                    min_units,
                    max_units,
                    point_value,
                )
            }
        };
        if units.is_finite() && units > 0.0 {
            Ok(units)
        } else {
            Err(EngineError::Sizing("the entry size is not finite and > 0"))
        }
    }
}

/// Everything one run needs beside the strategy, the data and the cost config.
#[derive(Debug, Clone, PartialEq)]
pub struct EngineConfig {
    /// The instrument traded (its series must be in the [`MarketData`]).
    pub instrument: String,
    /// Venue id in the cost config.
    pub venue: String,
    /// Stop/target evaluation mode (M1-R4).
    pub exit_evaluation: ExitEvaluation,
    /// Fixed quote → account rates.
    pub account_fx: AccountFx,
    /// Entry sizing.
    pub sizing: SizingPolicy,
    /// What happens to a position open at the end.
    pub end_of_run: EndOfRun,
    /// The trading window (inclusive UTC days), starting capital and risk-free rate.
    /// Bars before `first_day` are warm-up.
    pub metrics: MetricsConfig,
}

/// Counts of what the engine did, written with every trial.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct EngineStats {
    /// Primary bars stepped inside the window.
    pub bars: usize,
    /// Primary bars stepped before the window (warm-up).
    pub warmup_bars: usize,
    /// Signals the strategy emitted inside the window.
    pub signals: usize,
    /// Entries opened.
    pub entries: usize,
    /// Entries signalled during warm-up and rejected.
    pub warmup_entries_rejected: usize,
    /// Positions closed by the ledger's stop or target.
    pub ledger_exits: usize,
    /// Positions closed by a strategy exit.
    pub strategy_exits: usize,
    /// Stop moves applied.
    pub stop_moves: usize,
    /// Positions closed by [`EndOfRun::Close`].
    pub end_of_run_closes: usize,
    /// Entries filled on the final bar ([`FinalBarEntry`]); records written before the
    /// count existed read back as 0.
    #[serde(default)]
    pub final_bar_entries: usize,
}

/// A finished run.
#[derive(Debug, Clone)]
pub struct RunOutput {
    /// The book, with every fill, financing charge and closed trade.
    pub ledger: Ledger,
    /// M1-R5's metrics over the window.
    pub metrics: Metrics,
    /// What the engine did.
    pub stats: EngineStats,
}

/// Why a run failed. Nothing is retried or replaced by a fallback.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum EngineError {
    /// The data has no series for the instrument at a timeframe the strategy needs.
    #[error("no {timeframe} series for {instrument} in the market data")]
    MissingSeries {
        /// Instrument.
        instrument: String,
        /// Timeframe.
        timeframe: BarTimeframe,
    },
    /// The venue does not price the instrument.
    #[error("venue {venue:?} has no costs for {instrument:?}")]
    UnknownInstrument {
        /// Venue.
        venue: String,
        /// Instrument.
        instrument: String,
    },
    /// The bar feed rejected the series or the strategy rejected a bar.
    #[error(transparent)]
    Feed(#[from] RunError),
    /// The strategy rejected feedback.
    #[error(transparent)]
    Strategy(#[from] StrategyError),
    /// The ledger refused an operation (a divergent bar, no mark, ...).
    #[error(transparent)]
    Ledger(#[from] LedgerError),
    /// The strategy and the ledger are out of step, or a fill could not be bridged.
    #[error(transparent)]
    Bridge(#[from] BridgeError),
    /// An entry could not be sized.
    #[error("sizing: {0}")]
    Sizing(&'static str),
    /// The strategy's signals assume a different price scale from the instrument's
    /// ([`Strategy::price_scale`]): a signal that depends on it would be evaluated in
    /// the wrong units.
    #[error(
        "strategy {strategy} assumes raw_scale {assumed}, but {instrument} has raw_scale \
         {raw_scale}; configure the strategy with the instrument's raw_scale"
    )]
    PriceScale {
        /// Strategy name.
        strategy: &'static str,
        /// The scale the strategy assumes.
        assumed: f64,
        /// Instrument.
        instrument: String,
        /// The instrument's `raw_scale` in the cost config.
        raw_scale: f64,
    },
    /// The metrics could not be computed.
    #[error(transparent)]
    Metrics(#[from] MetricsError),
}

fn day_start(day: chrono::NaiveDate) -> DateTime<Utc> {
    day.and_time(NaiveTime::MIN).and_utc()
}

/// Run `strategy` over `data` under `config`, then compute the metrics. See the module
/// docs for the loop.
///
/// # Errors
/// [`EngineError`] for missing series, an instrument the venue does not price, or any
/// refusal during the run.
///
/// # Panics
/// Never: the feed steps the same primary series the loop iterates, and every
/// `expect` follows a check [`resolve_bar`] or [`Ledger::open`] has already made.
#[allow(
    clippy::too_many_lines,
    reason = "one bar loop; splitting it would scatter the per-bar order the module docs fix"
)]
pub fn run<S: Strategy + ?Sized>(
    strategy: &mut S,
    data: &MarketData,
    costs: &CostConfig,
    config: &EngineConfig,
) -> Result<RunOutput, EngineError> {
    let instrument = config.instrument.as_str();
    let series = |timeframe: BarTimeframe| {
        data.series(instrument, timeframe)
            .ok_or_else(|| EngineError::MissingSeries {
                instrument: instrument.to_owned(),
                timeframe,
            })
    };
    let primary_tf = strategy.primary_timeframe();
    let primary = series(primary_tf)?;
    let contexts = strategy
        .context_timeframes()
        .iter()
        .map(|&tf| series(tf))
        .collect::<Result<Vec<_>, _>>()?;
    let mut feed = BarFeed::new(strategy, primary, &contexts)?;

    let mut ledger = Ledger::new(
        costs,
        &config.venue,
        config.exit_evaluation,
        config.account_fx.clone(),
    )?;
    let model = ledger
        .fill_model(instrument)
        .ok_or_else(|| EngineError::UnknownInstrument {
            venue: config.venue.clone(),
            instrument: instrument.to_owned(),
        })?
        .clone();
    let bridge = FillBridge::new(&model)?;
    let raw_scale = model.spec().raw_scale;
    if let Some(assumed) = strategy.price_scale() {
        if (assumed - raw_scale).abs() > 1e-12 * raw_scale {
            return Err(EngineError::PriceScale {
                strategy: strategy.name(),
                assumed,
                instrument: instrument.to_owned(),
                raw_scale,
            });
        }
    }
    ledger.record_end_of_run(config.end_of_run);
    let trade_from = day_start(config.metrics.first_day);
    let trade_until = day_start(config.metrics.last_day) + chrono::Duration::days(1);

    let mut stats = EngineStats::default();
    let mut open: Option<PositionId> = None;
    let mut entered_at: Option<usize> = None;
    let mut last: Option<(usize, &JoinedBar)> = None;
    for (i, bar) in primary.bars().iter().enumerate() {
        if bar.ts_open_utc >= trade_until {
            break;
        }
        if bar.ts_open_utc < trade_from {
            let (_, signal) = feed.step(strategy)?.expect("the feed has this bar");
            stats.warmup_bars += 1;
            if let Some(signal) = signal {
                match resolve_bar(false, None, Some(&signal))? {
                    BarAction::Enter(_) => {
                        strategy.on_feedback(PositionFeedback::EntryRejected)?;
                        stats.warmup_entries_rejected += 1;
                    }
                    _ => return Err(BridgeError::OutOfStep("exit signalled while flat").into()),
                }
            }
            continue;
        }

        let trigger = match open {
            Some(id) => ledger.check_exit(id, bar)?,
            None => None,
        };
        let (stepped, signal) = feed.step(strategy)?.expect("the feed has this bar");
        debug_assert!(std::ptr::eq(stepped, bar));
        stats.bars += 1;
        stats.signals += usize::from(signal.is_some());
        let close_ts = bar.ts_close_utc(primary_tf);
        let at = |point: PricePoint, ts: DateTime<Utc>| FillAt {
            bar,
            point,
            ts,
            bar_index: i,
        };
        match resolve_bar(open.is_some(), trigger, signal.as_ref())? {
            BarAction::None => {}
            BarAction::Enter(entry) => {
                let units = config.sizing.units(&entry, &model, &config.account_fx)?;
                let order = bridge.open_order(instrument, &entry, units);
                let id = ledger.open(&order, at(PricePoint::Close, close_ts))?;
                let fill = ledger.fills().last().expect("open books a fill");
                let anchored = bridge.anchor(&entry, fill)?;
                strategy.on_feedback(anchored.feedback())?;
                ledger.set_levels(id, anchored.stop, anchored.target)?;
                open = Some(id);
                entered_at = Some(i);
                stats.entries += 1;
            }
            BarAction::LedgerExit {
                trigger,
                notify_strategy,
            } => {
                let id = open.take().expect("resolve_bar checked a position is open");
                let ts = match trigger.at {
                    PricePoint::Open => bar.ts_open_utc,
                    PricePoint::Close | PricePoint::Level(_) => close_ts,
                };
                ledger.close(id, at(trigger.at, ts), trigger.reason, data)?;
                if notify_strategy {
                    strategy.on_feedback(PositionFeedback::ClosedExternally)?;
                }
                stats.ledger_exits += 1;
            }
            BarAction::StrategyExit { reason } => {
                let id = open.take().expect("resolve_bar checked a position is open");
                ledger.close_signal(id, at(PricePoint::Close, close_ts), reason, data)?;
                stats.strategy_exits += 1;
            }
            BarAction::MoveStop { stop } => {
                let id = open.expect("resolve_bar checked a position is open");
                let target = ledger
                    .open_positions()
                    .iter()
                    .find(|p| p.id == id)
                    .and_then(|p| p.target);
                ledger.set_levels(id, Some(stop), target)?;
                stats.stop_moves += 1;
            }
        }
        if open.is_some() {
            ledger.accrue_financing(close_ts, data)?;
        }
        if open.is_some() != strategy.position().is_some() {
            return Err(BridgeError::OutOfStep("ledger and strategy disagree after a bar").into());
        }
        last = Some((i, bar));
    }

    if let (Some(_), Some(i), Some((last_i, _))) = (open, entered_at, last) {
        stats.final_bar_entries += usize::from(i == last_i);
    }
    if let (Some(id), Some((i, bar)), EndOfRun::Close) = (open, last, config.end_of_run) {
        let ts = bar.ts_close_utc(primary_tf);
        let at = FillAt {
            bar,
            point: PricePoint::Close,
            ts,
            bar_index: i,
        };
        ledger.close(id, at, ExitReason::EndOfRun, data)?;
        strategy.on_feedback(PositionFeedback::ClosedExternally)?;
        stats.end_of_run_closes += 1;
    }

    let metrics = Metrics::compute(&ledger, data, &config.metrics)?;
    Ok(RunOutput {
        ledger,
        metrics,
        stats,
    })
}
