//! The trade ledger: fills, open positions, financing charges and closed trades for one
//! run at one venue. Metrics (#31) consume it.
//!
//! Money model. Prices and P&L are f64 in the instrument's quote currency first. A run's
//! [`AccountFx`] holds one fixed quote → account (GBP) rate per currency, and every
//! account-currency figure is the quote figure times that rate. Commission is configured
//! and booked in the account currency. Size is in instrument units: P&L in quote
//! currency is `size × price move in quote units`. A spread-bet stake in pounds per point
//! maps to units through [`AccountFx::units_for_stake_per_point`].
//!
//! The ledger does not see the bar loop. The engine (#32) calls [`Ledger::open`],
//! [`Ledger::check_exit`], [`Ledger::close`] and [`Ledger::accrue_financing`] as bars
//! arrive, and #31 calls [`Ledger::mark_to_market`] at each day's close.

use std::collections::BTreeMap;

use chrono::{DateTime, NaiveDate, Utc};
use serde::{Deserialize, Serialize};

use crate::cost::config::{CostConfig, Exclusion, InstrumentSpec, Placeholder, PriceSide, Product};
use crate::cost::fill::{FillError, FillModel, PricePoint, TradeSide};
use crate::cost::financing::{Direction, RolloverCalendar, rollover_charge};
use crate::exit::{ExitEvaluation, ExitReason, ExitTrigger, SignalExitReason};
use crate::{JoinedBar, JoinedSeries, MarketData};

/// Why the ledger refused an operation. A refused operation changes nothing.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum LedgerError {
    /// The venue is not in the cost config.
    #[error("venue {0:?} is not defined in the cost config")]
    UnknownVenue(String),
    /// The run's venue has no costs for this instrument.
    #[error("venue {venue:?} defines no costs for instrument {instrument:?}")]
    UnknownInstrument {
        /// Venue id.
        venue: String,
        /// Instrument symbol.
        instrument: String,
    },
    /// No open position has this id.
    #[error("no open position {0:?}")]
    UnknownPosition(PositionId),
    /// The fill could not be priced.
    #[error(transparent)]
    Fill(#[from] FillError),
    /// A stop or target level is not finite.
    #[error("stop/target level {0} is not finite")]
    InvalidLevel(f64),
    /// The run's FX table has no rate for this currency.
    #[error("no {account} rate for quote currency {currency:?} in the run's AccountFx")]
    NoFxRate {
        /// The quote currency.
        currency: String,
        /// The account currency.
        account: String,
    },
    /// An FX rate is not finite and positive, or a currency code is malformed.
    #[error("FX rate for {currency:?} is invalid: {rate}")]
    InvalidFxRate {
        /// The currency.
        currency: String,
        /// The rate given.
        rate: f64,
    },
    /// The mark (the configured side's close) is not finite or not above zero: a corrupt
    /// bar. Nothing is booked, so financing and valuations never carry a `NaN`.
    #[error(
        "{instrument} mark from the bar opening {bar_open} is {price}, not a finite price above zero"
    )]
    BadMark {
        /// Instrument.
        instrument: String,
        /// Open of the bar the mark came from.
        bar_open: DateTime<Utc>,
        /// The mark found, quote units.
        price: f64,
    },
    /// No bar has closed for this instrument by the instant a mark is needed.
    #[error("no {instrument} bar has closed by {at} to mark from")]
    NoMark {
        /// Instrument.
        instrument: String,
        /// The instant needing a mark.
        at: DateTime<Utc>,
    },
    /// The exit is before the entry.
    #[error(
        "position {id:?} cannot exit at {exit} (bar {exit_bar}) before its entry at {entry} (bar {entry_bar})"
    )]
    ExitBeforeEntry {
        /// Position.
        id: PositionId,
        /// Entry timestamp.
        entry: DateTime<Utc>,
        /// Entry bar index.
        entry_bar: usize,
        /// Requested exit timestamp.
        exit: DateTime<Utc>,
        /// Requested exit bar index.
        exit_bar: usize,
    },
}

/// Fixed quote → account currency rates for one run.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AccountFx {
    account_currency: String,
    rates: BTreeMap<String, f64>,
}

impl AccountFx {
    /// A table converting each listed quote currency into `account_currency`: one unit of
    /// the quote currency is worth `rate` units of the account currency. The account
    /// currency converts to itself at 1.0 without being listed.
    ///
    /// # Errors
    /// [`LedgerError::InvalidFxRate`] for a rate that is not finite and positive, or a
    /// code that is not three upper-case letters.
    pub fn new(
        account_currency: impl Into<String>,
        rates: impl IntoIterator<Item = (String, f64)>,
    ) -> Result<Self, LedgerError> {
        let account_currency = account_currency.into();
        check_code(&account_currency, 1.0)?;
        let mut table = BTreeMap::new();
        for (currency, rate) in rates {
            check_code(&currency, rate)?;
            if !rate.is_finite() || rate <= 0.0 {
                return Err(LedgerError::InvalidFxRate { currency, rate });
            }
            table.insert(currency, rate);
        }
        table.insert(account_currency.clone(), 1.0);
        Ok(Self {
            account_currency,
            rates: table,
        })
    }

    /// The account currency.
    #[must_use]
    pub fn account_currency(&self) -> &str {
        &self.account_currency
    }

    /// Account-currency value of one unit of `currency`.
    ///
    /// # Errors
    /// [`LedgerError::NoFxRate`] when the run has no rate for `currency`.
    pub fn rate(&self, currency: &str) -> Result<f64, LedgerError> {
        self.rates
            .get(currency)
            .copied()
            .ok_or_else(|| LedgerError::NoFxRate {
                currency: currency.to_owned(),
                account: self.account_currency.clone(),
            })
    }

    /// Instrument units that make a one-pip/point move worth `stake` in the account
    /// currency: `stake / (pip_size × rate)`. A £1-per-point USA500 spread bet at
    /// USD→GBP 0.8 is 1.25 units.
    ///
    /// # Errors
    /// [`LedgerError::NoFxRate`] when the run has no rate for the quote currency.
    pub fn units_for_stake_per_point(
        &self,
        spec: &InstrumentSpec,
        stake: f64,
    ) -> Result<f64, LedgerError> {
        Ok(stake / (spec.pip_size * self.rate(&spec.quote_currency)?))
    }
}

fn check_code(code: &str, rate: f64) -> Result<(), LedgerError> {
    if code.len() == 3 && code.bytes().all(|b| b.is_ascii_uppercase()) {
        Ok(())
    } else {
        Err(LedgerError::InvalidFxRate {
            currency: code.to_owned(),
            rate,
        })
    }
}

/// What a run was configured with. #31 and #33 write it beside the results.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RunMetadata {
    /// Venue id from the cost config.
    pub venue: String,
    /// Venue display name.
    pub venue_name: String,
    /// Spread bet or CFD.
    pub product: Product,
    /// Stop/target evaluation mode and its both-hit sub-option (M1-R4).
    pub exit_evaluation: ExitEvaluation,
    /// The fixed FX table.
    pub account_fx: AccountFx,
    /// Every unverified cost component (spread, slippage, commission, financing or
    /// carry) configured for the venue.
    pub placeholders: Vec<Placeholder>,
    /// What the engine does with a position still open after the last bar, and with an
    /// entry signalled on the final bar (#59). `None` for a ledger the engine did not
    /// drive; it is then left out of the JSON, and records written before it existed
    /// read back as `None`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub end_of_run: Option<EndOfRunRule>,
    /// Every excluded cost component configured for the venue: what a result omits, or
    /// why it is uncosted. Absent in a record written before task #58.
    #[serde(default)]
    pub exclusions: Vec<Exclusion>,
    /// The venue's instruments whose spread is a published minimum, so that a result on
    /// them is a lower bound on spread cost.
    #[serde(default)]
    pub minimum_spreads: Vec<String>,
    /// Derived: for each instrument at the venue, whether a result on it may be reported
    /// as costed (no placeholder component, no spread or commission excluded). Empty in a
    /// record written before task #58, which therefore reads back as uncosted.
    #[serde(default)]
    pub costed: BTreeMap<String, bool>,
}

impl RunMetadata {
    /// Whether a result on `instrument` may be reported as costed. An instrument the
    /// metadata does not know is not costed.
    #[must_use]
    pub fn is_costed(&self, instrument: &str) -> bool {
        self.costed.get(instrument).copied().unwrap_or(false)
    }
}

/// What happens to a position still open after the last bar. Recorded with every trial.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EndOfRun {
    /// Close it at the last bar's close, booked as [`ExitReason::EndOfRun`] with the full
    /// exit cost.
    Close,
    /// Leave it open; the metrics value it at liquidation under `open_at_end`.
    LeaveOpen,
}

/// What happens to an entry signalled on the run's final bar.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FinalBarEntry {
    /// Filled at the final bar's close like an entry on any other bar (entry cost
    /// charged), then handled by [`EndOfRun`] like any other open position:
    /// [`EndOfRun::Close`] books it at that same close as [`ExitReason::EndOfRun`] with
    /// the full exit cost (a round trip held for zero bars), and [`EndOfRun::LeaveOpen`]
    /// carries it in `open_at_end` at its liquidation value.
    FilledThenEndOfRun,
}

/// The end-of-run rules a run was booked under, written into its [`RunMetadata`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct EndOfRunRule {
    /// Positions open after the last bar.
    pub mode: EndOfRun,
    /// An entry on the final bar.
    pub final_bar_entry: FinalBarEntry,
}

impl EndOfRunRule {
    /// The rules for a run whose open positions are handled by `mode`.
    #[must_use]
    pub fn new(mode: EndOfRun) -> Self {
        Self {
            mode,
            final_bar_entry: FinalBarEntry::FilledThenEndOfRun,
        }
    }
}

/// A ledger-assigned position id, sequential from 0 within a run.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct PositionId(pub u64);

/// Where a fill happens: the bar, the point in it, the fill's timestamp and the bar's
/// index in the run's stepping series (used for bars held).
#[derive(Debug, Clone, Copy)]
pub struct FillAt<'a> {
    /// The bar being filled on.
    pub bar: &'a JoinedBar,
    /// Open, close or a level.
    pub point: PricePoint,
    /// The fill's timestamp, chosen by the engine (e.g. the bar close for a close fill).
    pub ts: DateTime<Utc>,
    /// Index of `bar` in the series the engine steps through.
    pub bar_index: usize,
}

/// A request to open a position.
#[derive(Debug, Clone, PartialEq)]
pub struct OpenOrder {
    /// Instrument symbol.
    pub instrument: String,
    /// Long or short.
    pub direction: Direction,
    /// Size in instrument units (finite, greater than zero).
    pub size: f64,
    /// Stop level, raw units on the configured side.
    pub stop: Option<f64>,
    /// Target level, raw units on the configured side.
    pub target: Option<f64>,
}

/// One executed fill.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Fill {
    /// The position this fill opened or closed.
    pub position: PositionId,
    /// Fill timestamp.
    pub ts: DateTime<Utc>,
    /// Instrument symbol.
    pub instrument: String,
    /// Venue id.
    pub venue: String,
    /// Buy or sell.
    pub side: TradeSide,
    /// Size in instrument units.
    pub size: f64,
    /// The price side the reference was taken from.
    pub price_side: PriceSide,
    /// Reference price before costs, raw cache units.
    pub raw_price: f64,
    /// Reference price, quote units.
    pub mark_price: f64,
    /// Executed price, quote units.
    pub fill_price: f64,
    /// Half-spread × size, quote currency.
    pub spread_cost: f64,
    /// Slippage × size, quote currency.
    pub slippage_cost: f64,
    /// Commission, account currency (the closing fill includes the round-trip charge).
    pub commission: f64,
    /// Financing accrued over the position's life, quote currency; zero on an entry.
    pub financing: f64,
    /// `None` for an entry; the exit reason for a closing fill.
    pub exit_reason: Option<ExitReason>,
    /// The strategy's own label for a [`ExitReason::Signal`] close booked through
    /// [`Ledger::close_signal`]; absent otherwise.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub signal_reason: Option<SignalExitReason>,
}

/// One rollover charge booked against a position.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FinancingCharge {
    /// Position charged.
    pub position: PositionId,
    /// Instrument.
    pub instrument: String,
    /// The rollover instant.
    pub at_utc: DateTime<Utc>,
    /// The venue-local trading date it closes.
    pub local_date: NaiveDate,
    /// Calendar days charged.
    pub days: u32,
    /// Open of the bar whose configured-side close is the mark.
    pub mark_bar_open: DateTime<Utc>,
    /// Mark, quote units.
    pub mark_price: f64,
    /// Charge, quote currency (negative is a credit).
    pub charge: f64,
}

/// A position on the book.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OpenPosition {
    /// Position id.
    pub id: PositionId,
    /// Instrument symbol.
    pub instrument: String,
    /// Long or short.
    pub direction: Direction,
    /// Size in instrument units.
    pub size: f64,
    /// Entry timestamp.
    pub entry_ts: DateTime<Utc>,
    /// Entry bar index.
    pub entry_bar_index: usize,
    /// Entry reference price, raw units.
    pub entry_raw_price: f64,
    /// Entry reference price, quote units.
    pub entry_mark: f64,
    /// Entry executed price, quote units.
    pub entry_price: f64,
    /// Entry spread cost, quote currency.
    pub entry_spread_cost: f64,
    /// Entry slippage cost, quote currency.
    pub entry_slippage_cost: f64,
    /// Entry commission, account currency.
    pub entry_commission: f64,
    /// Stop level, raw units on the configured side.
    pub stop: Option<f64>,
    /// Target level, raw units on the configured side.
    pub target: Option<f64>,
    /// The last rollover charged (or the entry): only rollovers strictly after it remain.
    pub financed_through: DateTime<Utc>,
    /// Financing charged so far, quote currency.
    pub financing: f64,
    /// Calendar days charged so far.
    pub financing_days: u32,
}

/// A round trip.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClosedTrade {
    /// Position id.
    pub id: PositionId,
    /// Instrument symbol.
    pub instrument: String,
    /// Venue id.
    pub venue: String,
    /// The instrument's quote currency.
    pub quote_currency: String,
    /// Long or short.
    pub direction: Direction,
    /// Size in instrument units.
    pub size: f64,
    /// Entry timestamp.
    pub entry_ts: DateTime<Utc>,
    /// Exit timestamp.
    pub exit_ts: DateTime<Utc>,
    /// Bars between entry and exit on the engine's stepping series.
    pub bars_held: usize,
    /// Entry reference price, quote units.
    pub entry_mark: f64,
    /// Exit reference price, quote units.
    pub exit_mark: f64,
    /// Entry executed price, quote units.
    pub entry_price: f64,
    /// Exit executed price, quote units.
    pub exit_price: f64,
    /// Spread cost over both fills, quote currency.
    pub spread_cost: f64,
    /// Slippage cost over both fills, quote currency.
    pub slippage_cost: f64,
    /// Financing over the hold, quote currency (negative is a credit).
    pub financing: f64,
    /// Calendar days of financing charged.
    pub financing_days: u32,
    /// Commission over both fills and the round trip, account currency.
    pub commission: f64,
    /// `(exit_mark − entry_mark) × direction × size`, quote currency: before any cost.
    pub gross_pnl_quote: f64,
    /// Gross less spread, slippage, financing and commission, quote currency.
    pub net_pnl_quote: f64,
    /// The quote → account rate used.
    pub fx_rate: f64,
    /// `gross_pnl_quote × fx_rate`.
    pub gross_pnl_account: f64,
    /// `net_pnl_quote × fx_rate`.
    pub net_pnl_account: f64,
    /// Signal, stop, target or end of run.
    pub exit_reason: ExitReason,
    /// The strategy's label (Python `exit_reason`) for a signal exit booked through
    /// [`Ledger::close_signal`]; absent otherwise.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub signal_reason: Option<SignalExitReason>,
}

/// An open position valued at a mark.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PositionMark {
    /// Position.
    pub id: PositionId,
    /// Instrument.
    pub instrument: String,
    /// Open of the bar whose configured-side close is the mark.
    pub mark_bar_open: DateTime<Utc>,
    /// Mark, quote units.
    pub mark_price: f64,
    /// `(mark − entry_mark) × direction × size`, quote currency.
    pub unrealised_gross_quote: f64,
    /// `(mark − entry_price) × direction × size` less financing and entry commission,
    /// quote currency. Exit costs are not deducted.
    pub unrealised_net_quote: f64,
    /// `unrealised_gross_quote × fx_rate`.
    pub unrealised_gross_account: f64,
    /// `unrealised_net_quote × fx_rate`.
    pub unrealised_net_account: f64,
}

/// Where marks come from: the last bar of an instrument that has closed by an instant.
pub trait MarkSource {
    /// The latest bar of `symbol` whose close is at or before `at`.
    fn bar_closed_by(&self, symbol: &str, at: DateTime<Utc>) -> Option<&JoinedBar>;
}

/// `model`'s mark of `bar`, refused unless it is a finite price above zero.
pub(crate) fn checked_mark(model: &FillModel, bar: &JoinedBar) -> Result<f64, LedgerError> {
    let price = model.mark_close(bar);
    if price.is_finite() && price > 0.0 {
        Ok(price)
    } else {
        Err(LedgerError::BadMark {
            instrument: model.symbol().to_owned(),
            bar_open: bar.ts_open_utc,
            price,
        })
    }
}

fn last_closed_bar(series: &JoinedSeries, at: DateTime<Utc>) -> Option<&JoinedBar> {
    let length = series.timeframe().duration();
    let n = series
        .bars()
        .partition_point(|b| b.ts_open_utc + length <= at);
    n.checked_sub(1).map(|i| &series.bars()[i])
}

/// Marks from each instrument's 1-minute series, so a rollover is marked within a
/// minute of its instant whatever timeframe the strategy steps on.
impl MarkSource for MarketData {
    fn bar_closed_by(&self, symbol: &str, at: DateTime<Utc>) -> Option<&JoinedBar> {
        last_closed_bar(self.get(symbol)?.m1(), at)
    }
}

/// Marks from one series (for its own symbol only).
impl MarkSource for JoinedSeries {
    fn bar_closed_by(&self, symbol: &str, at: DateTime<Utc>) -> Option<&JoinedBar> {
        if symbol == self.symbol() {
            last_closed_bar(self, at)
        } else {
            None
        }
    }
}

/// The book for one run at one venue.
#[derive(Debug, Clone)]
pub struct Ledger {
    metadata: RunMetadata,
    calendar: RolloverCalendar,
    models: BTreeMap<String, FillModel>,
    next_id: u64,
    open: Vec<OpenPosition>,
    fills: Vec<Fill>,
    financing: Vec<FinancingCharge>,
    closed: Vec<ClosedTrade>,
}

impl Ledger {
    /// An empty ledger for `venue_id` in `config`.
    ///
    /// # Errors
    /// [`LedgerError::UnknownVenue`] when the venue is not in the config.
    pub fn new(
        config: &CostConfig,
        venue_id: &str,
        exit_evaluation: ExitEvaluation,
        account_fx: AccountFx,
    ) -> Result<Self, LedgerError> {
        let venue = config
            .venue(venue_id)
            .ok_or_else(|| LedgerError::UnknownVenue(venue_id.to_owned()))?;
        let mut models = BTreeMap::new();
        for (symbol, cost) in &venue.instruments {
            // Validation guarantees every venue instrument has a spec.
            if let Some(spec) = config.instrument(symbol) {
                models.insert(
                    symbol.clone(),
                    FillModel::new(symbol.clone(), spec.clone(), cost.clone()),
                );
            }
        }
        Ok(Self {
            metadata: RunMetadata {
                venue: venue_id.to_owned(),
                venue_name: venue.name.clone(),
                product: venue.product,
                exit_evaluation,
                account_fx,
                placeholders: config
                    .placeholders(venue_id)
                    .map_err(|_| LedgerError::UnknownVenue(venue_id.to_owned()))?,
                end_of_run: None,
                exclusions: config
                    .exclusions(venue_id)
                    .map_err(|_| LedgerError::UnknownVenue(venue_id.to_owned()))?,
                minimum_spreads: config
                    .minimum_spreads(venue_id)
                    .map_err(|_| LedgerError::UnknownVenue(venue_id.to_owned()))?,
                costed: config
                    .costed(venue_id)
                    .map_err(|_| LedgerError::UnknownVenue(venue_id.to_owned()))?,
            },
            calendar: RolloverCalendar::new(venue.rollover, venue.weekend),
            models,
            next_id: 0,
            open: Vec::new(),
            fills: Vec::new(),
            financing: Vec::new(),
            closed: Vec::new(),
        })
    }

    /// The run's metadata.
    #[must_use]
    pub fn metadata(&self) -> &RunMetadata {
        &self.metadata
    }

    /// Record the end-of-run rules this ledger is booked under (the engine does, before
    /// its first bar), so they travel with the run's results.
    pub fn record_end_of_run(&mut self, mode: EndOfRun) {
        self.metadata.end_of_run = Some(EndOfRunRule::new(mode));
    }

    /// The fill model for `symbol` at this venue.
    #[must_use]
    pub fn fill_model(&self, symbol: &str) -> Option<&FillModel> {
        self.models.get(symbol)
    }

    /// Every fill, in booking order.
    #[must_use]
    pub fn fills(&self) -> &[Fill] {
        &self.fills
    }

    /// Every rollover charge, in booking order.
    #[must_use]
    pub fn financing_charges(&self) -> &[FinancingCharge] {
        &self.financing
    }

    /// Positions still open, in opening order.
    #[must_use]
    pub fn open_positions(&self) -> &[OpenPosition] {
        &self.open
    }

    /// Round trips, in closing order.
    #[must_use]
    pub fn closed_trades(&self) -> &[ClosedTrade] {
        &self.closed
    }

    fn model(&self, symbol: &str) -> Result<&FillModel, LedgerError> {
        self.models
            .get(symbol)
            .ok_or_else(|| LedgerError::UnknownInstrument {
                venue: self.metadata.venue.clone(),
                instrument: symbol.to_owned(),
            })
    }

    fn position_index(&self, id: PositionId) -> Result<usize, LedgerError> {
        self.open
            .iter()
            .position(|p| p.id == id)
            .ok_or(LedgerError::UnknownPosition(id))
    }

    /// Open a position with a fill at `at`.
    ///
    /// # Errors
    /// [`LedgerError::UnknownInstrument`], [`LedgerError::InvalidLevel`],
    /// [`LedgerError::NoFxRate`], or [`LedgerError::Fill`] when the fill cannot be priced
    /// (including a divergent bar).
    pub fn open(&mut self, order: &OpenOrder, at: FillAt<'_>) -> Result<PositionId, LedgerError> {
        let model = self.model(&order.instrument)?;
        for level in [order.stop, order.target].into_iter().flatten() {
            if !level.is_finite() {
                return Err(LedgerError::InvalidLevel(level));
            }
        }
        self.metadata
            .account_fx
            .rate(&model.spec().quote_currency)?;
        let side = match order.direction {
            Direction::Long => TradeSide::Buy,
            Direction::Short => TradeSide::Sell,
        };
        let q = model.quote(at.bar, at.point, at.ts, side, order.size)?;
        let price_side = model.price_side();
        let id = PositionId(self.next_id);
        self.next_id += 1;
        self.fills.push(Fill {
            position: id,
            ts: at.ts,
            instrument: order.instrument.clone(),
            venue: self.metadata.venue.clone(),
            side,
            size: order.size,
            price_side,
            raw_price: q.raw_price,
            mark_price: q.mark_price,
            fill_price: q.fill_price,
            spread_cost: q.spread_cost,
            slippage_cost: q.slippage_cost,
            commission: q.commission,
            financing: 0.0,
            exit_reason: None,
            signal_reason: None,
        });
        self.open.push(OpenPosition {
            id,
            instrument: order.instrument.clone(),
            direction: order.direction,
            size: order.size,
            entry_ts: at.ts,
            entry_bar_index: at.bar_index,
            entry_raw_price: q.raw_price,
            entry_mark: q.mark_price,
            entry_price: q.fill_price,
            entry_spread_cost: q.spread_cost,
            entry_slippage_cost: q.slippage_cost,
            entry_commission: q.commission,
            stop: order.stop,
            target: order.target,
            financed_through: at.ts,
            financing: 0.0,
            financing_days: 0,
        });
        Ok(id)
    }

    /// Replace a position's stop and target (a trailing stop, for example).
    ///
    /// # Errors
    /// [`LedgerError::UnknownPosition`] or [`LedgerError::InvalidLevel`].
    pub fn set_levels(
        &mut self,
        id: PositionId,
        stop: Option<f64>,
        target: Option<f64>,
    ) -> Result<(), LedgerError> {
        for level in [stop, target].into_iter().flatten() {
            if !level.is_finite() {
                return Err(LedgerError::InvalidLevel(level));
            }
        }
        let i = self.position_index(id)?;
        self.open[i].stop = stop;
        self.open[i].target = target;
        Ok(())
    }

    /// Evaluate `bar` against a position's stop and target under the run's
    /// [`ExitEvaluation`]. The caller closes the position at the returned point.
    ///
    /// # Errors
    /// [`LedgerError::UnknownPosition`].
    pub fn check_exit(
        &self,
        id: PositionId,
        bar: &JoinedBar,
    ) -> Result<Option<ExitTrigger>, LedgerError> {
        let p = &self.open[self.position_index(id)?];
        let reference = self.model(&p.instrument)?.reference(bar);
        Ok(self
            .metadata
            .exit_evaluation
            .evaluate(&reference, p.direction, p.stop, p.target))
    }

    /// The rollover charges position `i` owes for rollovers strictly before `now`.
    fn pending_financing(
        &self,
        i: usize,
        now: DateTime<Utc>,
        marks: &impl MarkSource,
    ) -> Result<Vec<FinancingCharge>, LedgerError> {
        let p = &self.open[i];
        let model = self.model(&p.instrument)?;
        let rollovers =
            self.calendar
                .rollovers_between(model.spec().asset_class, p.financed_through, now);
        rollovers
            .into_iter()
            .map(|r| {
                let bar = marks
                    .bar_closed_by(&p.instrument, r.at_utc)
                    .ok_or_else(|| LedgerError::NoMark {
                        instrument: p.instrument.clone(),
                        at: r.at_utc,
                    })?;
                let mark = checked_mark(model, bar)?;
                Ok(FinancingCharge {
                    position: p.id,
                    instrument: p.instrument.clone(),
                    at_utc: r.at_utc,
                    local_date: r.local_date,
                    days: r.days,
                    mark_bar_open: bar.ts_open_utc,
                    mark_price: mark,
                    charge: rollover_charge(
                        &model.cost().financing,
                        p.direction,
                        p.size,
                        mark,
                        r.days,
                    ),
                })
            })
            .collect()
    }

    fn book_financing(&mut self, i: usize, charges: Vec<FinancingCharge>) {
        let p = &mut self.open[i];
        for c in &charges {
            p.financing += c.charge;
            p.financing_days += c.days;
            p.financed_through = c.at_utc;
        }
        self.financing.extend(charges);
    }

    /// Charge every open position for each rollover it has been held across, up to (not
    /// including) `now`. Calling it again for the same span charges nothing more.
    ///
    /// # Errors
    /// [`LedgerError::NoMark`] when no bar has closed by a rollover to mark from, and
    /// [`LedgerError::BadMark`] when that bar's mark is not a finite price above zero;
    /// nothing is booked in either case.
    pub fn accrue_financing(
        &mut self,
        now: DateTime<Utc>,
        marks: &impl MarkSource,
    ) -> Result<(), LedgerError> {
        let mut all = Vec::with_capacity(self.open.len());
        for i in 0..self.open.len() {
            all.push(self.pending_financing(i, now, marks)?);
        }
        for (i, charges) in all.into_iter().enumerate() {
            self.book_financing(i, charges);
        }
        Ok(())
    }

    /// Close a position with a fill at `at`, first charging any rollovers it was held
    /// across before `at.ts`.
    ///
    /// # Errors
    /// [`LedgerError::UnknownPosition`], [`LedgerError::ExitBeforeEntry`],
    /// [`LedgerError::Fill`] (including a divergent bar) or [`LedgerError::NoMark`]. On
    /// error the position stays open and nothing is booked.
    pub fn close(
        &mut self,
        id: PositionId,
        at: FillAt<'_>,
        reason: ExitReason,
        marks: &impl MarkSource,
    ) -> Result<&ClosedTrade, LedgerError> {
        self.close_with(id, at, reason, None, marks)
    }

    /// Close a position for a strategy exit: booked as [`ExitReason::Signal`], with the
    /// strategy's label kept on the closing [`Fill`] and the [`ClosedTrade`]. Otherwise
    /// exactly [`Ledger::close`].
    ///
    /// # Errors
    /// As [`Ledger::close`].
    pub fn close_signal(
        &mut self,
        id: PositionId,
        at: FillAt<'_>,
        reason: SignalExitReason,
        marks: &impl MarkSource,
    ) -> Result<&ClosedTrade, LedgerError> {
        self.close_with(id, at, reason.into(), Some(reason), marks)
    }

    fn close_with(
        &mut self,
        id: PositionId,
        at: FillAt<'_>,
        reason: ExitReason,
        signal_reason: Option<SignalExitReason>,
        marks: &impl MarkSource,
    ) -> Result<&ClosedTrade, LedgerError> {
        let i = self.position_index(id)?;
        let p = &self.open[i];
        let bars_held = at
            .bar_index
            .checked_sub(p.entry_bar_index)
            .filter(|_| at.ts >= p.entry_ts)
            .ok_or(LedgerError::ExitBeforeEntry {
                id,
                entry: p.entry_ts,
                entry_bar: p.entry_bar_index,
                exit: at.ts,
                exit_bar: at.bar_index,
            })?;
        let model = self.model(&p.instrument)?;
        let side = match p.direction {
            Direction::Long => TradeSide::Sell,
            Direction::Short => TradeSide::Buy,
        };
        let q = model.quote(at.bar, at.point, at.ts, side, p.size)?;
        let quote_currency = model.spec().quote_currency.clone();
        let price_side = model.price_side();
        let round_trip = model.cost().commission.per_round_trip;
        let fx_rate = self.metadata.account_fx.rate(&quote_currency)?;
        let charges = self.pending_financing(i, at.ts, marks)?;

        self.book_financing(i, charges);
        let p = self.open.remove(i);
        let exit_commission = q.commission + round_trip;
        let commission = p.entry_commission + exit_commission;
        let s = p.direction.sign();
        let gross_pnl_quote = (q.mark_price - p.entry_mark) * s * p.size;
        let net_pnl_quote =
            (q.fill_price - p.entry_price) * s * p.size - p.financing - commission / fx_rate;
        self.fills.push(Fill {
            position: id,
            ts: at.ts,
            instrument: p.instrument.clone(),
            venue: self.metadata.venue.clone(),
            side,
            size: p.size,
            price_side,
            raw_price: q.raw_price,
            mark_price: q.mark_price,
            fill_price: q.fill_price,
            spread_cost: q.spread_cost,
            slippage_cost: q.slippage_cost,
            commission: exit_commission,
            financing: p.financing,
            exit_reason: Some(reason),
            signal_reason,
        });
        self.closed.push(ClosedTrade {
            id,
            instrument: p.instrument,
            venue: self.metadata.venue.clone(),
            quote_currency,
            direction: p.direction,
            size: p.size,
            entry_ts: p.entry_ts,
            exit_ts: at.ts,
            bars_held,
            entry_mark: p.entry_mark,
            exit_mark: q.mark_price,
            entry_price: p.entry_price,
            exit_price: q.fill_price,
            spread_cost: p.entry_spread_cost + q.spread_cost,
            slippage_cost: p.entry_slippage_cost + q.slippage_cost,
            financing: p.financing,
            financing_days: p.financing_days,
            commission,
            gross_pnl_quote,
            net_pnl_quote,
            fx_rate,
            gross_pnl_account: gross_pnl_quote * fx_rate,
            net_pnl_account: net_pnl_quote * fx_rate,
            exit_reason: reason,
            signal_reason,
        });
        Ok(&self.closed[self.closed.len() - 1])
    }

    /// Value every open position at the configured-side close of the last bar that has
    /// closed by `at`. Financing is as booked: call [`Ledger::accrue_financing`] with the
    /// same `at` first to include rollovers up to it.
    ///
    /// # Errors
    /// [`LedgerError::NoMark`] when an instrument has no bar closed by `at`, and
    /// [`LedgerError::BadMark`] when its mark is not a finite price above zero.
    pub fn mark_to_market(
        &self,
        at: DateTime<Utc>,
        marks: &impl MarkSource,
    ) -> Result<Vec<PositionMark>, LedgerError> {
        self.open
            .iter()
            .map(|p| {
                let model = self.model(&p.instrument)?;
                let bar =
                    marks
                        .bar_closed_by(&p.instrument, at)
                        .ok_or_else(|| LedgerError::NoMark {
                            instrument: p.instrument.clone(),
                            at,
                        })?;
                let mark = checked_mark(model, bar)?;
                let rate = self
                    .metadata
                    .account_fx
                    .rate(&model.spec().quote_currency)?;
                let s = p.direction.sign();
                let gross = (mark - p.entry_mark) * s * p.size;
                let net =
                    (mark - p.entry_price) * s * p.size - p.financing - p.entry_commission / rate;
                Ok(PositionMark {
                    id: p.id,
                    instrument: p.instrument.clone(),
                    mark_bar_open: bar.ts_open_utc,
                    mark_price: mark,
                    unrealised_gross_quote: gross,
                    unrealised_net_quote: net,
                    unrealised_gross_account: gross * rate,
                    unrealised_net_account: net * rate,
                })
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cost::config::{CostComponent, ExclusionEffect, ProvenanceStatus};
    use crate::exit::BothHit;
    use crate::{BarTimeframe, Ohlcv};
    use chrono::{Duration, TimeZone};

    fn utc(y: i32, mo: u32, d: u32, h: u32, mi: u32) -> DateTime<Utc> {
        Utc.with_ymd_and_hms(y, mo, d, h, mi, 0).unwrap()
    }

    fn flat(p: f64) -> Ohlcv {
        Ohlcv {
            open: p,
            high: p,
            low: p,
            close: p,
            tick_volume: 1.0,
        }
    }

    /// Hourly USA500 bars, bid = mid − 0.25 and ask = mid + 0.25, mid stepping by `step`
    /// points an hour from `start_mid`.
    fn spx_series(from: DateTime<Utc>, hours: i64, start_mid: f64, step: f64) -> JoinedSeries {
        let bars = (0..hours)
            .map(|h| {
                #[allow(clippy::cast_precision_loss)]
                let mid = start_mid + step * h as f64;
                JoinedBar {
                    ts_open_utc: from + Duration::hours(h),
                    bid: flat(mid - 0.25),
                    ask: flat(mid + 0.25),
                }
            })
            .collect();
        JoinedSeries::new("USA500IDXUSD", BarTimeframe::H1, bars).unwrap()
    }

    fn fx() -> AccountFx {
        AccountFx::new("GBP", [("USD".to_owned(), 0.8), ("EUR".to_owned(), 0.85)]).unwrap()
    }

    fn ledger(eval: ExitEvaluation) -> Ledger {
        Ledger::new(
            &crate::cost::config::arithmetic_fixture(),
            "ig_spread_bet",
            eval,
            fx(),
        )
        .unwrap()
    }

    const STOP_FIRST: ExitEvaluation = ExitEvaluation::Intrabar {
        both_hit: BothHit::StopFirst,
    };

    fn order(direction: Direction, size: f64) -> OpenOrder {
        OpenOrder {
            instrument: "USA500IDXUSD".to_owned(),
            direction,
            size,
            stop: None,
            target: None,
        }
    }

    fn close_to(a: f64, b: f64) -> bool {
        (a - b).abs() < 1e-9 * a.abs().max(b.abs()).max(1.0)
    }

    #[test]
    fn a_non_finite_or_non_positive_mark_is_refused_not_booked() {
        // #48: a long held across Tuesday's 22:00Z rollover, marked from the 21:00 bar.
        let from = utc(2024, 1, 16, 12, 0);
        for bad in [f64::NAN, f64::INFINITY, -5.0, 0.0] {
            let mut bars = spx_series(from, 12, 4800.0, 1.0).bars().to_vec();
            assert_eq!(bars[9].ts_open_utc, utc(2024, 1, 16, 21, 0));
            bars[9].bid.close = bad;
            bars[9].ask.close = bad;
            let s = JoinedSeries::new("USA500IDXUSD", BarTimeframe::H1, bars).unwrap();
            let mut l = ledger(STOP_FIRST);
            let entry_bar = &s.bars()[0];
            l.open(
                &order(Direction::Long, 2.0),
                FillAt {
                    bar: entry_bar,
                    point: PricePoint::Open,
                    ts: entry_bar.ts_open_utc,
                    bar_index: 0,
                },
            )
            .unwrap();
            let err = l.accrue_financing(utc(2024, 1, 16, 23, 0), &s).unwrap_err();
            assert!(
                matches!(err, LedgerError::BadMark { ref instrument, bar_open, .. }
                    if instrument == "USA500IDXUSD" && bar_open == utc(2024, 1, 16, 21, 0)),
                "{bad}: {err}"
            );
            assert!(l.financing_charges().is_empty(), "nothing booked for {bad}");
            assert_eq!(l.open_positions()[0].financing, 0.0);
            assert!(matches!(
                l.mark_to_market(utc(2024, 1, 16, 22, 0), &s),
                Err(LedgerError::BadMark { .. })
            ));
        }
    }

    #[test]
    fn round_trip_with_financing_books_every_field() {
        // Tuesday 2024-01-16 12:00Z to Thursday 12:00Z: two IG rollovers (Tue and Wed
        // 22:00 GMT), each one day for an index.
        let from = utc(2024, 1, 16, 12, 0);
        let s = spx_series(from, 49, 4800.0, 1.0);
        let mut l = ledger(STOP_FIRST);
        let entry_bar = &s.bars()[0];
        let id = l
            .open(
                &order(Direction::Long, 2.0),
                FillAt {
                    bar: entry_bar,
                    point: PricePoint::Open,
                    ts: entry_bar.ts_open_utc,
                    bar_index: 0,
                },
            )
            .unwrap();
        let exit_bar = &s.bars()[48];
        let t = l
            .close(
                id,
                FillAt {
                    bar: exit_bar,
                    point: PricePoint::Close,
                    ts: exit_bar.ts_close_utc(BarTimeframe::H1),
                    bar_index: 48,
                },
                ExitReason::Signal,
                &s,
            )
            .unwrap()
            .clone();

        // Marks: entry 4800 (open of bar 0), exit 4848 (close of bar 48, flat bars).
        assert!(close_to(t.entry_mark, 4800.0));
        assert!(close_to(t.exit_mark, 4848.0));
        // IG USA500: 0.4-point spread (0.2 half) + 0.1 slippage per fill.
        assert!(close_to(t.entry_price, 4800.3));
        assert!(close_to(t.exit_price, 4847.7));
        assert!(close_to(t.gross_pnl_quote, 96.0));
        assert!(close_to(t.spread_cost, 2.0 * 0.2 * 2.0));
        assert!(close_to(t.slippage_cost, 2.0 * 0.1 * 2.0));
        // Rollovers Tue 22:00Z (mark = close of the 21:00 bar, mid 4809) and Wed 22:00Z
        // (mid 4833); 3.4% / 360, one day each, size 2.
        let expect_fin = 2.0 * (4809.0 + 4833.0) * 0.034 / 360.0;
        assert_eq!(t.financing_days, 2);
        assert!(close_to(t.financing, expect_fin));
        let charges = l.financing_charges();
        assert_eq!(charges.len(), 2);
        assert_eq!(charges[0].at_utc, utc(2024, 1, 16, 22, 0));
        assert_eq!(charges[0].mark_bar_open, utc(2024, 1, 16, 21, 0));
        assert!(close_to(charges[0].mark_price, 4809.0));
        assert_eq!(t.commission, 0.0);
        assert!(close_to(t.net_pnl_quote, 96.0 - 0.8 - 0.4 - expect_fin));
        assert!(close_to(
            t.net_pnl_quote,
            t.gross_pnl_quote - t.spread_cost - t.slippage_cost - t.financing
        ));
        assert_eq!(t.fx_rate, 0.8);
        assert!(close_to(t.net_pnl_account, t.net_pnl_quote * 0.8));
        assert!(close_to(t.gross_pnl_account, 96.0 * 0.8));
        assert_eq!(t.bars_held, 48);
        assert_eq!(t.exit_reason, ExitReason::Signal);
        assert_eq!(t.quote_currency, "USD");

        // Fills: entry buy then exit sell; financing rides on the exit fill.
        let f = l.fills();
        assert_eq!(f.len(), 2);
        assert_eq!((f[0].side, f[1].side), (TradeSide::Buy, TradeSide::Sell));
        assert_eq!(f[0].financing, 0.0);
        assert!(close_to(f[1].financing, expect_fin));
        assert_eq!(f[1].exit_reason, Some(ExitReason::Signal));
        assert_eq!(f[0].price_side, PriceSide::Mid);
        assert!(close_to(f[0].raw_price, 4800.0));
        assert_eq!(f[0].venue, "ig_spread_bet");
        assert!(l.open_positions().is_empty());
        assert_eq!(l.closed_trades().len(), 1);
    }

    #[test]
    fn commission_round_trip_and_fx_flow_into_net_pnl() {
        let c = crate::cost::config::arithmetic_fixture();
        let mut l = Ledger::new(&c, "pepperstone_razor", ExitEvaluation::CloseOnly, fx()).unwrap();
        // EURUSD intraday, no rollover: 100,000 units, mid 1.1000 -> 1.1050.
        let at = |h, mid: f64| JoinedBar {
            ts_open_utc: utc(2024, 1, 16, h, 0),
            bid: flat(mid - 0.05),
            ask: flat(mid + 0.05),
        };
        let (b0, b1) = (at(9, 11_000.0), at(15, 11_050.0));
        let order = OpenOrder {
            instrument: "EURUSD".to_owned(),
            direction: Direction::Long,
            size: 100_000.0,
            stop: None,
            target: None,
        };
        let id = l
            .open(
                &order,
                FillAt {
                    bar: &b0,
                    point: PricePoint::Close,
                    ts: b0.ts_open_utc,
                    bar_index: 0,
                },
            )
            .unwrap();
        let s = JoinedSeries::new("EURUSD", BarTimeframe::H1, vec![b0, b1]).unwrap();
        let t = l
            .close(
                id,
                FillAt {
                    bar: &b1,
                    point: PricePoint::Close,
                    ts: b1.ts_open_utc,
                    bar_index: 6,
                },
                ExitReason::Target,
                &s,
            )
            .unwrap();
        // Gross 0.0050 x 100,000 = 500 USD. Razor: 0.1-pip spread, 0.2-pip slippage.
        assert!(close_to(t.gross_pnl_quote, 500.0));
        assert!(close_to(t.spread_cost, 2.0 * 0.000_005 * 100_000.0));
        assert!(close_to(t.slippage_cost, 2.0 * 0.000_02 * 100_000.0));
        // GBP 2.25 each side.
        assert!(close_to(t.commission, 4.5));
        assert_eq!(t.financing, 0.0);
        let net_quote = 500.0 - 1.0 - 4.0 - 4.5 / 0.8;
        assert!(close_to(t.net_pnl_quote, net_quote));
        assert!(close_to(t.net_pnl_account, net_quote * 0.8));
    }

    #[test]
    fn short_positions_pay_the_short_rate_and_profit_on_a_fall() {
        let from = utc(2024, 1, 19, 12, 0); // Friday
        let s = spx_series(from, 75, 4800.0, -1.0); // to Monday 15:00Z
        // The first 3.4% index entry in the file is USA500IDXUSD's.
        let config_text = crate::cost::config::BUILTIN_COSTS_TOML.replacen(
            "long_rate = 0.034, short_rate = 0.034,",
            "long_rate = 0.034, short_rate = -0.01,",
            1,
        );
        assert_ne!(config_text, crate::cost::config::BUILTIN_COSTS_TOML);
        let c = CostConfig::from_toml_str(&config_text).unwrap();
        assert_eq!(
            c.venue("ig_spread_bet").unwrap().instruments["USA500IDXUSD"]
                .financing
                .short_rate,
            -0.01
        );
        let mut l = Ledger::new(&c, "ig_spread_bet", STOP_FIRST, fx()).unwrap();
        let b0 = &s.bars()[0];
        let id = l
            .open(
                &order(Direction::Short, 1.0),
                FillAt {
                    bar: b0,
                    point: PricePoint::Open,
                    ts: b0.ts_open_utc,
                    bar_index: 0,
                },
            )
            .unwrap();
        let last = &s.bars()[74];
        let t = l
            .close(
                id,
                FillAt {
                    bar: last,
                    point: PricePoint::Close,
                    ts: last.ts_close_utc(BarTimeframe::H1),
                    bar_index: 74,
                },
                ExitReason::EndOfRun,
                &s,
            )
            .unwrap();
        // One rollover (Friday 22:00Z, triple) at mark = close of the 21:00 bar, mid 4791.
        assert_eq!(t.financing_days, 3);
        assert!(close_to(t.financing, 4791.0 * -0.01 / 360.0 * 3.0));
        assert!(t.gross_pnl_quote > 0.0);
        assert_eq!(t.exit_reason, ExitReason::EndOfRun);
    }

    #[test]
    fn accrual_is_idempotent_and_close_does_not_recharge() {
        let from = utc(2024, 1, 15, 12, 0);
        let s = spx_series(from, 24 * 8, 4800.0, 0.0);
        let mut l = ledger(STOP_FIRST);
        let b0 = &s.bars()[0];
        let id = l
            .open(
                &order(Direction::Long, 1.0),
                FillAt {
                    bar: b0,
                    point: PricePoint::Open,
                    ts: b0.ts_open_utc,
                    bar_index: 0,
                },
            )
            .unwrap();
        for h in [5, 30, 30, 31, 100, 100, 150] {
            l.accrue_financing(from + Duration::hours(h), &s).unwrap();
        }
        let before = l.open_positions()[0].financing_days;
        let last = &s.bars()[24 * 7];
        let t = l
            .close(
                id,
                FillAt {
                    bar: last,
                    point: PricePoint::Open,
                    ts: last.ts_open_utc,
                    bar_index: 24 * 7,
                },
                ExitReason::Signal,
                &s,
            )
            .unwrap();
        // Monday 12:00Z to the next Monday 12:00Z: exactly 7 days, each charged once.
        assert_eq!(t.financing_days, 7);
        assert!(before <= 7);
        let dates: Vec<_> = l.financing_charges().iter().map(|c| c.local_date).collect();
        let mut unique = dates.clone();
        unique.dedup();
        assert_eq!(dates, unique);
        assert_eq!(dates.len(), 5);
    }

    #[test]
    fn mark_to_market_values_open_positions_at_the_last_closed_bar() {
        let from = utc(2024, 1, 16, 12, 0);
        let s = spx_series(from, 24, 4800.0, 2.0);
        let mut l = ledger(STOP_FIRST);
        let b0 = &s.bars()[0];
        l.open(
            &order(Direction::Long, 3.0),
            FillAt {
                bar: b0,
                point: PricePoint::Close,
                ts: b0.ts_close_utc(BarTimeframe::H1),
                bar_index: 0,
            },
        )
        .unwrap();
        // At 18:30Z the last closed hourly bar opened 17:00 (mid 4810).
        let m = l.mark_to_market(utc(2024, 1, 16, 18, 30), &s).unwrap();
        assert_eq!(m.len(), 1);
        assert_eq!(m[0].mark_bar_open, utc(2024, 1, 16, 17, 0));
        assert!(close_to(m[0].mark_price, 4810.0));
        assert!(close_to(m[0].unrealised_gross_quote, 3.0 * 10.0));
        assert!(close_to(m[0].unrealised_net_quote, 3.0 * (4810.0 - 4800.3)));
        assert!(close_to(
            m[0].unrealised_net_account,
            0.8 * 3.0 * (4810.0 - 4800.3)
        ));
        // Before any bar has closed there is no mark.
        assert!(matches!(
            l.mark_to_market(utc(2024, 1, 16, 12, 30), &s),
            Err(LedgerError::NoMark { .. })
        ));
    }

    #[test]
    fn stop_and_target_checks_use_the_run_mode() {
        let mut l = ledger(STOP_FIRST);
        let b = JoinedBar {
            ts_open_utc: utc(2024, 1, 16, 14, 0),
            bid: Ohlcv {
                open: 4799.75,
                high: 4811.75,
                low: 4789.75,
                close: 4800.75,
                tick_volume: 1.0,
            },
            ask: Ohlcv {
                open: 4800.25,
                high: 4812.25,
                low: 4790.25,
                close: 4801.25,
                tick_volume: 1.0,
            },
        };
        let o = OpenOrder {
            stop: Some(4795.0),
            target: Some(4810.0),
            ..order(Direction::Long, 1.0)
        };
        let id = l
            .open(
                &o,
                FillAt {
                    bar: &b,
                    point: PricePoint::Open,
                    ts: b.ts_open_utc,
                    bar_index: 0,
                },
            )
            .unwrap();
        let trig = l.check_exit(id, &b).unwrap().unwrap();
        assert_eq!(trig.reason, ExitReason::Stop);
        assert_eq!(trig.at, PricePoint::Level(4795.0));
        l.set_levels(id, None, Some(4811.0)).unwrap();
        let trig = l.check_exit(id, &b).unwrap().unwrap();
        assert_eq!(trig.reason, ExitReason::Target);
        assert_eq!(l.metadata().exit_evaluation, STOP_FIRST);
        assert!(matches!(
            l.set_levels(id, Some(f64::NAN), None),
            Err(LedgerError::InvalidLevel(_))
        ));
    }

    #[test]
    fn refused_operations_change_nothing() {
        let s = spx_series(utc(2024, 1, 16, 12, 0), 4, 4800.0, 0.0);
        let mut l = ledger(STOP_FIRST);
        let b = &s.bars()[2];
        let at = FillAt {
            bar: b,
            point: PricePoint::Open,
            ts: b.ts_open_utc,
            bar_index: 2,
        };
        assert!(matches!(
            l.open(
                &OpenOrder {
                    instrument: "GBPUSD".to_owned(),
                    ..order(Direction::Long, 1.0)
                },
                at
            ),
            Err(LedgerError::UnknownInstrument { .. })
        ));
        assert!(matches!(
            l.open(&order(Direction::Long, 0.0), at),
            Err(LedgerError::Fill(FillError::InvalidSize(_)))
        ));
        let id = l.open(&order(Direction::Long, 1.0), at).unwrap();
        let early = &s.bars()[1];
        assert!(matches!(
            l.close(
                id,
                FillAt {
                    bar: early,
                    point: PricePoint::Open,
                    ts: early.ts_open_utc,
                    bar_index: 1
                },
                ExitReason::Signal,
                &s
            ),
            Err(LedgerError::ExitBeforeEntry { .. })
        ));
        // A divergent exit bar is refused and the position stays open.
        let bad = JoinedBar {
            ts_open_utc: utc(2024, 1, 16, 15, 0),
            bid: flat(4800.0),
            ask: flat(4900.0),
        };
        assert!(matches!(
            l.close(
                id,
                FillAt {
                    bar: &bad,
                    point: PricePoint::Close,
                    ts: utc(2024, 1, 16, 16, 0),
                    bar_index: 3
                },
                ExitReason::Signal,
                &s
            ),
            Err(LedgerError::Fill(FillError::Divergence { .. }))
        ));
        assert_eq!(l.open_positions().len(), 1);
        assert_eq!(l.fills().len(), 1);
        assert!(matches!(
            l.close(PositionId(99), at, ExitReason::Signal, &s),
            Err(LedgerError::UnknownPosition(_))
        ));
        // No FX rate for the quote currency.
        let mut no_usd = Ledger::new(
            &crate::cost::config::arithmetic_fixture(),
            "ig_spread_bet",
            STOP_FIRST,
            AccountFx::new("GBP", []).unwrap(),
        )
        .unwrap();
        assert!(matches!(
            no_usd.open(&order(Direction::Long, 1.0), at),
            Err(LedgerError::NoFxRate { .. })
        ));
    }

    #[test]
    fn financing_needs_a_mark_and_books_nothing_without_one() {
        let s = spx_series(utc(2024, 1, 16, 12, 0), 4, 4800.0, 0.0);
        let mut l = ledger(STOP_FIRST);
        let b = &s.bars()[0];
        l.open(
            &order(Direction::Long, 1.0),
            FillAt {
                bar: b,
                point: PricePoint::Open,
                ts: b.ts_open_utc,
                bar_index: 0,
            },
        )
        .unwrap();
        // The series stops at 16:00Z; the 22:00Z rollover's mark is the 15:00 bar.
        l.accrue_financing(utc(2024, 1, 17, 0, 0), &s).unwrap();
        assert_eq!(
            l.financing_charges()[0].mark_bar_open,
            utc(2024, 1, 16, 15, 0)
        );
        // A source that knows nothing about the instrument refuses.
        let other = JoinedSeries::new("EURUSD", BarTimeframe::H1, vec![]).unwrap();
        assert!(matches!(
            l.accrue_financing(utc(2024, 1, 18, 0, 0), &other),
            Err(LedgerError::NoMark { .. })
        ));
        assert_eq!(l.financing_charges().len(), 1);
    }

    #[test]
    fn metadata_records_the_mode_fx_and_the_builtin_provenance() {
        let l = Ledger::new(
            &CostConfig::builtin(),
            "pepperstone_spread_bet",
            ExitEvaluation::Intrabar {
                both_hit: BothHit::Conservative,
            },
            fx(),
        )
        .unwrap();
        let m = l.metadata();
        assert_eq!(m.venue, "pepperstone_spread_bet");
        assert_eq!(m.product, Product::SpreadBet);
        assert_eq!(
            m.exit_evaluation,
            ExitEvaluation::Intrabar {
                both_hit: BothHit::Conservative
            }
        );
        assert_eq!(m.account_fx.account_currency(), "GBP");
        // No builtin figure is a placeholder (task #58).
        assert!(m.placeholders.is_empty(), "{:?}", m.placeholders);
        // Excluded: slippage and carry for all five instruments, the FX and gold funding
        // mark-up (EURUSD, GBPJPY, XAUUSD), and the GBPJPY spread-bet spread.
        let count = |c| m.exclusions.iter().filter(|e| e.component == c).count();
        assert_eq!(count(CostComponent::Slippage), 5);
        assert_eq!(count(CostComponent::Carry), 5);
        assert_eq!(count(CostComponent::Financing), 3);
        assert_eq!(count(CostComponent::Spread), 1);
        assert_eq!(count(CostComponent::Commission), 0);
        let spread = m
            .exclusions
            .iter()
            .find(|e| e.component == CostComponent::Spread)
            .unwrap();
        assert_eq!(spread.instrument, "GBPJPY");
        assert_eq!(spread.effect, ExclusionEffect::Uncosted);
        assert!(m.exclusions.iter().all(|e| !e.reason.is_empty()));
        // An excluded spread makes GBPJPY uncosted; the rest are costed, on minimums.
        assert!(!m.is_costed("GBPJPY"));
        for s in ["EURUSD", "XAUUSD", "USA500IDXUSD", "DEUIDXEUR"] {
            assert!(m.is_costed(s), "{s}");
        }
        assert!(!m.is_costed("NOWHERE"));
        assert_eq!(
            m.minimum_spreads,
            ["DEUIDXEUR", "EURUSD", "USA500IDXUSD", "XAUUSD"]
        );
        let toml = toml::to_string(m).unwrap();
        assert!(toml.contains("mode = \"intrabar\""), "{toml}");
        assert!(toml.contains("both_hit = \"conservative\""), "{toml}");
        assert!(toml.contains("effect = \"uncosted\""), "{toml}");
        assert!(toml.contains("GBPJPY = false"), "{toml}");
        assert!(matches!(
            Ledger::new(&CostConfig::builtin(), "nowhere", STOP_FIRST, fx()),
            Err(LedgerError::UnknownVenue(_))
        ));
    }

    #[test]
    fn run_metadata_lists_verified_excluded_and_placeholder_per_component() {
        // The builtin IG EURUSD entry with its spread turned back into a placeholder.
        let text = crate::cost::config::BUILTIN_COSTS_TOML.replacen(
            "spread = { pips = 1.04, basis = \"average\", placeholder = false",
            "spread = { pips = 1.04, basis = \"average\", placeholder = true",
            1,
        );
        let config = CostConfig::from_toml_str(&text).unwrap();
        let cost = &config.venue("ig_spread_bet").unwrap().instruments["EURUSD"];
        let statuses: Vec<(CostComponent, ProvenanceStatus)> = cost
            .provenance()
            .iter()
            .map(|p| (p.component, p.status))
            .collect();
        assert_eq!(
            statuses,
            [
                (CostComponent::Spread, ProvenanceStatus::Placeholder),
                (CostComponent::Slippage, ProvenanceStatus::Excluded),
                (CostComponent::Commission, ProvenanceStatus::Verified),
                (CostComponent::Financing, ProvenanceStatus::Verified),
                (CostComponent::Carry, ProvenanceStatus::Excluded),
            ]
        );
        let l = Ledger::new(&config, "ig_spread_bet", STOP_FIRST, fx()).unwrap();
        let m = l.metadata();
        // The placeholder is listed and makes EURUSD uncosted; nothing else changes.
        let placeholders: Vec<(&str, CostComponent)> = m
            .placeholders
            .iter()
            .map(|p| (p.instrument.as_str(), p.component))
            .collect();
        assert_eq!(placeholders, [("EURUSD", CostComponent::Spread)]);
        let eurusd: Vec<(CostComponent, ExclusionEffect)> = m
            .exclusions
            .iter()
            .filter(|e| e.instrument == "EURUSD")
            .map(|e| (e.component, e.effect))
            .collect();
        assert_eq!(
            eurusd,
            [
                (CostComponent::Slippage, ExclusionEffect::Omitted),
                (CostComponent::Carry, ExclusionEffect::Omitted),
            ]
        );
        assert!(!m.is_costed("EURUSD"));
        for s in ["GBPJPY", "XAUUSD", "USA500IDXUSD", "DEUIDXEUR"] {
            assert!(m.is_costed(s), "{s}");
        }
        // IG's FX spreads are averages; gold and the index schedules are minimums.
        assert_eq!(m.minimum_spreads, ["DEUIDXEUR", "USA500IDXUSD", "XAUUSD"]);
        // Every IG instrument is costed in the builtin config.
        let builtin =
            Ledger::new(&CostConfig::builtin(), "ig_spread_bet", STOP_FIRST, fx()).unwrap();
        assert!(builtin.metadata().costed.values().all(|c| *c));
        assert_eq!(builtin.metadata().costed.len(), 5);
    }

    #[test]
    fn metadata_written_before_the_costed_fields_reads_back_uncosted() {
        let l = ledger(STOP_FIRST);
        let mut value = serde_json::to_value(l.metadata()).unwrap();
        let map = value.as_object_mut().unwrap();
        for key in ["exclusions", "minimum_spreads", "costed"] {
            assert!(map.remove(key).is_some(), "{key}");
        }
        let old: RunMetadata = serde_json::from_value(value).unwrap();
        assert!(old.exclusions.is_empty() && old.costed.is_empty());
        assert!(!old.is_costed("USA500IDXUSD"));
    }

    #[test]
    fn account_fx_rejects_bad_rates_and_converts_stakes() {
        assert!(AccountFx::new("GBP", [("USD".to_owned(), 0.0)]).is_err());
        assert!(AccountFx::new("GBP", [("usd".to_owned(), 0.8)]).is_err());
        assert!(AccountFx::new("pounds", []).is_err());
        let fx = fx();
        assert_eq!(fx.rate("GBP").unwrap(), 1.0);
        let c = crate::cost::config::arithmetic_fixture();
        let units = fx
            .units_for_stake_per_point(c.instrument("USA500IDXUSD").unwrap(), 1.0)
            .unwrap();
        assert!(close_to(units, 1.25));
        // £1 per pip on EURUSD: 1 pip x units x 0.8 = £1.
        let units = fx
            .units_for_stake_per_point(c.instrument("EURUSD").unwrap(), 1.0)
            .unwrap();
        assert!(close_to(units * 0.0001 * 0.8, 1.0));
    }

    #[test]
    fn the_mark_is_the_last_bar_closed_at_or_before_the_instant() {
        let s = JoinedSeries::new(
            "EURUSD",
            BarTimeframe::M1,
            vec![
                JoinedBar {
                    ts_open_utc: utc(2024, 1, 16, 21, 58),
                    bid: flat(1.0),
                    ask: flat(1.0),
                },
                JoinedBar {
                    ts_open_utc: utc(2024, 1, 16, 21, 59),
                    bid: flat(2.0),
                    ask: flat(2.0),
                },
                JoinedBar {
                    ts_open_utc: utc(2024, 1, 16, 22, 0),
                    bid: flat(3.0),
                    ask: flat(3.0),
                },
            ],
        )
        .unwrap();
        // The 21:59 bar closes exactly at 22:00 and is the rollover mark.
        assert_eq!(
            last_closed_bar(&s, utc(2024, 1, 16, 22, 0))
                .unwrap()
                .bid
                .close,
            2.0
        );
        assert!(last_closed_bar(&s, utc(2024, 1, 16, 21, 58)).is_none());
    }
}
