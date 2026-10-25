//! The venue cost configuration: per-instrument units and per-venue, per-instrument costs.
//!
//! The checked-in `crates/tradedesk-backtest/config/costs.toml` is a labelled example: its
//! venue figures are illustrative, not any venue's. It loads with [`CostConfig::builtin`].
//! A real run loads its own file with the same schema through [`CostConfig::from_path`] or
//! [`CostConfig::from_toml_str`]. Every load is validated
//! before it is returned, so a [`CostConfig`] in hand is always usable.
//!
//! Units, once:
//! - **Raw** prices are the cache's on-disk numbers (EURUSD `11036.6`, XAUUSD in cents).
//!   `raw_scale` turns them into **quote** prices (EURUSD `1.10366`, XAUUSD `2063.625`).
//! - A **pip** (FX, metals) or **point** (indices) is `pip_size` in quote price. Spreads,
//!   fixed slippage and the divergence tolerance are in pips/points.
//! - Commissions are in the **account** currency. Financing rates are annual fractions,
//!   positive for a charge to the trader and negative for a credit.
//!
//! Provenance is per component: the spread, slippage, commission, financing and carry of
//! every venue × instrument, and its optional spread excess and dividend, each carry their
//! own `source` and a status. A component is **verified** (a published venue figure), a
//! **placeholder** (`placeholder = true`: an assumption or stand-in), **excluded**
//! (`excluded = true` with a `reason`: no published figure exists, so the component
//! charges nothing) or **estimated** (`estimated = true`, slippage, spread excess, carry
//! and dividend only: derived from data, and costed). [`CostConfig::placeholders`], [`CostConfig::exclusions`] and
//! [`CostConfig::estimates`] list them, and [`CostConfig::costed`] says which instruments
//! can be reported as costed (see [`CostComponent::exclusion_effect`]).

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use chrono::{DateTime, NaiveDate, NaiveTime, Timelike, Utc, Weekday};
use chrono_tz::Tz;
use serde::{Deserialize, Deserializer, Serialize};
use sha2::{Digest, Sha256};

/// The checked-in example cost configuration (see the module docs).
pub const BUILTIN_COSTS_TOML: &str = include_str!("../../config/costs.toml");

/// Why a cost configuration cannot be loaded or used.
#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    /// The file could not be read.
    #[error("cannot read cost config {path}: {source}")]
    Io {
        /// The path that failed.
        path: PathBuf,
        /// The underlying I/O error.
        source: std::io::Error,
    },
    /// The TOML did not match the schema (unknown field, wrong type, missing field).
    #[error("cost config does not parse: {0}")]
    Parse(#[from] toml::de::Error),
    /// A value parsed but is not allowed (negative spread, unknown instrument, …).
    #[error("cost config is invalid at {at}: {reason}")]
    Invalid {
        /// Dotted path to the offending entry.
        at: String,
        /// What is wrong with it.
        reason: String,
    },
    /// The requested venue is not in the config.
    #[error("venue {0:?} is not defined in the cost config")]
    UnknownVenue(String),
    /// The venue does not define costs for the requested instrument.
    #[error("venue {venue:?} defines no costs for instrument {instrument:?}")]
    UnknownInstrument {
        /// Venue id.
        venue: String,
        /// Instrument symbol.
        instrument: String,
    },
}

/// Asset class, which selects the weekend financing rule.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AssetClass {
    /// Spot FX pairs.
    Fx,
    /// Spot metals (gold, silver).
    Metal,
    /// Cash index markets.
    Index,
    /// Energy commodities (crude oil, natural gas, refined products). Its weekend rule is
    /// optional in a venue: required only where the venue costs an energy instrument.
    Energy,
}

/// Which price of the joined bar a venue × instrument marks and fills from.
///
/// The fill adds half the configured venue spread to this price for a buy and subtracts
/// it for a sell, so the configured side is treated as the venue's mid reference.
/// `Mid` is the natural choice; `Bid` or `Ask` are offered for parity experiments.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PriceSide {
    /// Dukascopy bid.
    Bid,
    /// Dukascopy ask.
    Ask,
    /// `(bid + ask) / 2`.
    Mid,
}

/// What the venue sells: a spread bet or a CFD. Recorded for the audit trail; the cost
/// arithmetic is the same for both (see the money-model Decision on issue #30).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Product {
    /// A UK spread bet, staked in account currency per point.
    SpreadBet,
    /// A contract for difference, sized in instrument units.
    Cfd,
}

/// A weekday on which a rollover can carry the weekend.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TripleDay {
    /// Monday.
    Monday,
    /// Tuesday.
    Tuesday,
    /// Wednesday (spot FX: T+2 settlement rolls Wednesday's value date over the weekend).
    Wednesday,
    /// Thursday.
    Thursday,
    /// Friday.
    Friday,
}

impl TripleDay {
    /// The chrono weekday.
    #[must_use]
    pub fn weekday(self) -> Weekday {
        match self {
            Self::Monday => Weekday::Mon,
            Self::Tuesday => Weekday::Tue,
            Self::Wednesday => Weekday::Wed,
            Self::Thursday => Weekday::Thu,
            Self::Friday => Weekday::Fri,
        }
    }
}

/// How one asset class's weekend is charged at a venue.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WeekendRule {
    /// The rollover that carries the weekend.
    pub triple_day: TripleDay,
    /// Days charged at that rollover (3 at every venue defined today: the day itself
    /// plus Saturday and Sunday). Every other weekday rollover charges 1.
    #[serde(default = "three")]
    pub days_on_triple: u32,
}

fn three() -> u32 {
    3
}

/// The weekend rule for each asset class. `fx`, `metal` and `index` are required.
/// `energy` is optional: a loaded config has it in every venue that costs an energy
/// instrument, so an existing config without energy instruments loads unchanged.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WeekendRules {
    /// Spot FX.
    pub fx: WeekendRule,
    /// Spot metals.
    pub metal: WeekendRule,
    /// Indices.
    pub index: WeekendRule,
    /// Energy commodities.
    #[serde(default)]
    pub energy: Option<WeekendRule>,
}

impl WeekendRules {
    /// The rule for `class`, or `None` when the venue defines none for it (only
    /// [`AssetClass::Energy`] can be absent).
    #[must_use]
    pub fn for_class(&self, class: AssetClass) -> Option<WeekendRule> {
        match class {
            AssetClass::Fx => Some(self.fx),
            AssetClass::Metal => Some(self.metal),
            AssetClass::Index => Some(self.index),
            AssetClass::Energy => self.energy,
        }
    }
}

/// The venue's daily rollover instant, in its own local time zone.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RolloverSpec {
    /// IANA zone, e.g. `"Europe/London"`.
    #[serde(deserialize_with = "de_tz")]
    pub tz: Tz,
    /// Local wall-clock time, `"HH:MM"`.
    #[serde(deserialize_with = "de_time")]
    pub time: NaiveTime,
}

fn de_tz<'de, D: Deserializer<'de>>(d: D) -> Result<Tz, D::Error> {
    let s = String::deserialize(d)?;
    s.parse::<Tz>()
        .map_err(|_| serde::de::Error::custom(format!("unknown IANA time zone {s:?}")))
}

fn de_opt_tz<'de, D: Deserializer<'de>>(d: D) -> Result<Option<Tz>, D::Error> {
    de_tz(d).map(Some)
}

fn de_time<'de, D: Deserializer<'de>>(d: D) -> Result<NaiveTime, D::Error> {
    let s = String::deserialize(d)?;
    NaiveTime::parse_from_str(&s, "%H:%M")
        .map_err(|_| serde::de::Error::custom(format!("rollover time {s:?} is not HH:MM")))
}

fn de_opt_time<'de, D: Deserializer<'de>>(d: D) -> Result<Option<NaiveTime>, D::Error> {
    let s = String::deserialize(d)?;
    NaiveTime::parse_from_str(&s, "%H:%M")
        .map(Some)
        .map_err(|_| serde::de::Error::custom(format!("time {s:?} is not HH:MM")))
}

/// How an instrument's prices are stored and measured. Venue-independent.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InstrumentSpec {
    /// Asset class (selects the weekend financing rule).
    pub asset_class: AssetClass,
    /// ISO currency the instrument is quoted in; P&L is in this currency first.
    pub quote_currency: String,
    /// Quote price = raw cache price × `raw_scale`.
    pub raw_scale: f64,
    /// One pip (FX, metals) or point (indices) in quote price.
    pub pip_size: f64,
    /// Largest |ask − bid| on a bar, in pips/points, that a fill accepts. Beyond it the
    /// bar is treated as corrupt and the fill is an error.
    pub divergence_tolerance: f64,
}

impl InstrumentSpec {
    /// A raw cache price in quote units.
    #[must_use]
    pub fn to_quote(&self, raw: f64) -> f64 {
        raw * self.raw_scale
    }

    /// A distance in pips/points as a quote-price distance.
    #[must_use]
    pub fn pips_to_quote(&self, pips: f64) -> f64 {
        pips * self.pip_size
    }

    /// A raw-price distance in pips/points.
    #[must_use]
    pub fn raw_to_pips(&self, raw_distance: f64) -> f64 {
        raw_distance * self.raw_scale / self.pip_size
    }
}

/// What a published spread figure is.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SpreadBasis {
    /// The venue's average over a stated period.
    Average,
    /// The venue's minimum, "from" or standard figure: the spread can only be wider, so a
    /// result costed on it is a lower bound on spread cost.
    Minimum,
}

/// One time-of-day window of a spread schedule, in the schedule's local time zone.
#[derive(Debug, Clone, Copy, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SpreadWindow {
    /// Local start, `"HH:MM"`, inclusive.
    #[serde(deserialize_with = "de_time")]
    pub from: NaiveTime,
    /// Local end, `"HH:MM"`, exclusive. Earlier than `from` means the window wraps
    /// midnight.
    #[serde(deserialize_with = "de_time")]
    pub to: NaiveTime,
    /// Full spread inside the window, in pips/points.
    pub pips: f64,
}

impl SpreadWindow {
    /// Whether local wall-clock time `t` falls in `[from, to)`, wrapping midnight when
    /// `to` is earlier than `from`.
    #[must_use]
    pub fn contains(&self, t: NaiveTime) -> bool {
        if self.from <= self.to {
            self.from <= t && t < self.to
        } else {
            t >= self.from || t < self.to
        }
    }
}

/// The venue spread for one instrument.
///
/// With no `schedule`, `pips` applies at every hour. With one, the window containing the
/// fill instant (in `tz`) sets the spread, and `pips` applies outside every window.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Spread {
    /// Full bid-to-offer width, in pips/points (outside every schedule window).
    pub pips: f64,
    /// Whether the figures are averages or minimums. Required unless excluded.
    #[serde(default)]
    pub basis: Option<SpreadBasis>,
    /// The time zone the schedule's windows are in. Required with a schedule.
    #[serde(default, deserialize_with = "de_opt_tz")]
    pub tz: Option<Tz>,
    /// Time-of-day windows, which must not overlap.
    #[serde(default)]
    pub schedule: Vec<SpreadWindow>,
    /// Where the figure comes from.
    pub source: String,
    /// `true` when the figure is not a verified venue number.
    pub placeholder: bool,
    /// `true` when no published figure exists. The spread is then zero and the venue ×
    /// instrument is never reported as costed.
    #[serde(default)]
    pub excluded: bool,
    /// Why the component is excluded. Required with `excluded`, and only then.
    #[serde(default)]
    pub reason: Option<String>,
}

impl Spread {
    /// The full spread, in pips/points, for a fill at `at`.
    #[must_use]
    pub fn pips_at(&self, at: DateTime<Utc>) -> f64 {
        if let Some(tz) = self.tz {
            let local = at.with_timezone(&tz).time();
            if let Some(w) = self.schedule.iter().find(|w| w.contains(local)) {
                return w.pips;
            }
        }
        self.pips
    }
}

/// How the per-fill slippage is computed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SlippageModel {
    /// A fixed amount per fill: `points` pips/points plus `bps` basis points of the mark.
    #[default]
    Fixed,
    /// A coefficient times the price range of the one-minute bar the order executes in
    /// (see [`crate::cost::fill`]): `market`, `stop` or `limit` by fill kind.
    Volatility,
}

/// Adverse slippage applied on every fill.
///
/// With [`SlippageModel::Fixed`] (the default) it is `points` and `bps`. With
/// [`SlippageModel::Volatility`] it is `market`, `stop` or `limit` times the one-minute
/// range at the execution minute, never less than the median range of the last
/// `floor_minutes` minutes.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Slippage {
    /// `fixed` (default) or `volatility`.
    #[serde(default)]
    pub model: SlippageModel,
    /// Fixed slippage per fill, in pips/points.
    #[serde(default)]
    pub points: f64,
    /// Proportional slippage per fill, in basis points of the mark.
    #[serde(default)]
    pub bps: f64,
    /// `volatility` only: the coefficient for market fills (entries, signal exits,
    /// end-of-run closes and the liquidation valuation). Dimensionless.
    #[serde(default)]
    pub market: Option<f64>,
    /// `volatility` only: the coefficient for stop fills, gap fills included.
    #[serde(default)]
    pub stop: Option<f64>,
    /// `volatility` only: the coefficient for target (limit) fills, gap fills included.
    #[serde(default)]
    pub limit: Option<f64>,
    /// `volatility` only: how many one-minute bars before the fill the floor's median
    /// covers. Default 30.
    #[serde(default)]
    pub floor_minutes: Option<u32>,
    /// `volatility` only: how many minutes after a market fill instant its execution
    /// minute may open. Default 5.
    #[serde(default)]
    pub max_gap_minutes: Option<u32>,
    /// Where the figures come from.
    pub source: String,
    /// `true` when the figures are an assumption, not a verified venue number.
    pub placeholder: bool,
    /// `true` when no published figure exists: no slippage is charged, and costed results
    /// omit it.
    #[serde(default)]
    pub excluded: bool,
    /// Why the component is excluded. Required with `excluded`, and only then.
    #[serde(default)]
    pub reason: Option<String>,
    /// `true` when the figures are estimated from data rather than published: still
    /// costed, and listed by [`CostConfig::estimates`].
    #[serde(default)]
    pub estimated: bool,
}

/// Default of [`Slippage::floor_minutes`].
pub const DEFAULT_FLOOR_MINUTES: u32 = 30;
/// Default of [`Slippage::max_gap_minutes`].
pub const DEFAULT_MAX_GAP_MINUTES: u32 = 5;
/// Default of [`SpreadExcess::baseline_weekdays`].
pub const DEFAULT_BASELINE_WEEKDAYS: u32 = 20;

impl Slippage {
    /// The floor window in one-minute bars ([`DEFAULT_FLOOR_MINUTES`] when unset).
    #[must_use]
    pub fn floor_minutes(&self) -> u32 {
        self.floor_minutes.unwrap_or(DEFAULT_FLOOR_MINUTES)
    }

    /// The market-fill gap in minutes ([`DEFAULT_MAX_GAP_MINUTES`] when unset).
    #[must_use]
    pub fn max_gap_minutes(&self) -> u32 {
        self.max_gap_minutes.unwrap_or(DEFAULT_MAX_GAP_MINUTES)
    }
}

/// An extra charge when the fill minute's spread is wider than usual: the configured
/// venue half-spread times `(min(w, cap) − 1)⁺`, where `w` is the store's ask − bid at the
/// execution minute over its median at the same time of day on recent weekdays. The
/// store's ask − bid is only that ratio; it is never the cost (see [`crate::cost::fill`]).
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SpreadExcess {
    /// Weekdays in the baseline median. Default 20, at least 2.
    #[serde(default)]
    pub baseline_weekdays: Option<u32>,
    /// The largest ratio charged. Finite, at least 1.
    pub cap: f64,
    /// Where the figures come from.
    pub source: String,
    /// `true` when the figures are an assumption.
    pub placeholder: bool,
    /// `true` when the component is not modelled: nothing is charged.
    #[serde(default)]
    pub excluded: bool,
    /// Why the component is excluded. Required with `excluded`, and only then.
    #[serde(default)]
    pub reason: Option<String>,
    /// `true` when the figures are estimated from data.
    #[serde(default)]
    pub estimated: bool,
}

impl SpreadExcess {
    /// The baseline's weekday count ([`DEFAULT_BASELINE_WEEKDAYS`] when unset).
    #[must_use]
    pub fn baseline_weekdays(&self) -> u32 {
        self.baseline_weekdays.unwrap_or(DEFAULT_BASELINE_WEEKDAYS)
    }
}

/// Commission, in account currency. Each amount defaults to zero; the provenance does
/// not, so a zero commission is a stated fact with a source, never an omission.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Commission {
    /// A fixed amount on every fill.
    #[serde(default)]
    pub per_fill: f64,
    /// An amount per instrument unit of size on every fill.
    #[serde(default)]
    pub per_unit_per_fill: f64,
    /// A fixed amount once per round trip, charged on the closing fill.
    #[serde(default)]
    pub per_round_trip: f64,
    /// Where the figures come from.
    pub source: String,
    /// `true` when the figures are not a verified venue number.
    pub placeholder: bool,
    /// `true` when no published figure exists. No commission is charged, and the venue ×
    /// instrument is never reported as costed.
    #[serde(default)]
    pub excluded: bool,
    /// Why the component is excluded. Required with `excluded`, and only then.
    #[serde(default)]
    pub reason: Option<String>,
}

/// Overnight financing for one venue × instrument: the venue's own annual charge (its
/// admin fee or mark-up). The benchmark or tom-next part is [`Carry`].
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Financing {
    /// Annual rate charged on a long position (negative is a credit).
    pub long_rate: f64,
    /// Annual rate charged on a short position (negative is a credit).
    pub short_rate: f64,
    /// Day-count divisor: 365 or 360.
    pub day_count: u32,
    /// Where the figures come from.
    pub source: String,
    /// `true` when the rates are not a verified venue figure.
    pub placeholder: bool,
    /// `true` when no published annual rate exists: nothing is charged, and costed
    /// results omit it.
    #[serde(default)]
    pub excluded: bool,
    /// Why the component is excluded. Required with `excluded`, and only then.
    #[serde(default)]
    pub reason: Option<String>,
}

/// The benchmark-rate or tom-next part of overnight financing: the signed interest carry
/// a venue adds to or subtracts from its admin fee.
///
/// Two forms are valid. **Excluded** (`excluded = true` with a `reason`): nothing is
/// charged, and costed results omit it. **Modelled** (`estimated = true` with `table`,
/// `sha256` and `day_count`): a dated rate table ([`CarryTable`]) that the ledger charges
/// at each rollover, on the same calendar as [`Financing`]. The table's sha256 is checked
/// when the config loads.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Carry {
    /// Where the venue states how the carry is set, or the inputs of the table.
    pub source: String,
    /// Must be `false`: an excluded or modelled carry is not a placeholder.
    pub placeholder: bool,
    /// `true` when the carry is not modelled: nothing is charged.
    #[serde(default)]
    pub excluded: bool,
    /// Why it is excluded and what costed results therefore omit.
    #[serde(default)]
    pub reason: Option<String>,
    /// `true` for the modelled form: the table is an estimate. Still costed, and listed by
    /// [`CostConfig::estimates`].
    #[serde(default)]
    pub estimated: bool,
    /// Modelled form: the rate table's CSV file. A relative path resolves against the
    /// cost file's directory (see [`CostConfig::from_toml_str_at`]).
    #[serde(default)]
    pub table: Option<PathBuf>,
    /// Modelled form: the sha256 of the table file, 64 hex digits (either case).
    #[serde(default)]
    pub sha256: Option<String>,
    /// Modelled form: the carry's own day-count divisor, 365 or 360.
    #[serde(default)]
    pub day_count: Option<u32>,
    /// The table, read and checked at load. `None` for an excluded carry.
    #[serde(skip)]
    pub rates: Option<Arc<CarryTable>>,
}

impl Carry {
    /// Whether the carry is the modelled form (a loaded rate table).
    #[must_use]
    pub fn is_modelled(&self) -> bool {
        self.rates.is_some()
    }
}

/// The CSV header a carry table must have, in this order.
pub const CARRY_TABLE_HEADER: [&str; 6] = [
    "date",
    "long_rate",
    "short_rate",
    "long_rate_date",
    "short_rate_date",
    "flag",
];

/// One row of a carry table: the annual rates charged at the rollover that closes the
/// venue-local trading date `date`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CarryRow {
    /// The venue-local trading date of the rollover.
    pub date: NaiveDate,
    /// Annual rate on a long position (positive is a charge, negative a credit).
    pub long_rate: f64,
    /// Annual rate on a short position (positive is a charge, negative a credit).
    pub short_rate: f64,
    /// The publication date of the input behind `long_rate`; never after `date`.
    pub long_rate_date: NaiveDate,
    /// The publication date of the input behind `short_rate`; never after `date`.
    pub short_rate_date: NaiveDate,
    /// A free-text note from the table's maker (for example `stale`); may be empty.
    pub flag: String,
}

/// A carry table: rows by date, ascending, one per date.
#[derive(Debug, Clone, PartialEq)]
pub struct CarryTable {
    sha256: String,
    rows: Vec<CarryRow>,
}

impl CarryTable {
    /// Parse a table from its file bytes and check them against `sha256` (lower-case or
    /// upper-case hex).
    ///
    /// # Errors
    /// A message for a sha256 mismatch, a file that is not UTF-8, a header other than
    /// [`CARRY_TABLE_HEADER`], a row that does not parse, a non-finite rate, a date that
    /// is not after the row before it, or a rate date after its row's date.
    pub fn from_bytes(bytes: &[u8], sha256: &str) -> Result<Self, String> {
        let found = sha256_hex(bytes);
        if !found.eq_ignore_ascii_case(sha256) {
            return Err(format!("sha256 is {found}, but the config says {sha256}"));
        }
        let mut reader = csv::ReaderBuilder::new()
            .has_headers(true)
            .trim(csv::Trim::All)
            .from_reader(bytes);
        let header = reader.headers().map_err(|e| format!("header: {e}"))?;
        if header.iter().ne(CARRY_TABLE_HEADER) {
            return Err(format!(
                "header is {:?}, expected {:?}",
                header.iter().collect::<Vec<_>>().join(","),
                CARRY_TABLE_HEADER.join(",")
            ));
        }
        let mut rows: Vec<CarryRow> = Vec::new();
        for (i, record) in reader.deserialize::<CarryRow>().enumerate() {
            // Line 1 is the header.
            let line = i + 2;
            let row = record.map_err(|e| format!("line {line}: {e}"))?;
            for (name, rate) in [("long_rate", row.long_rate), ("short_rate", row.short_rate)] {
                if !rate.is_finite() {
                    return Err(format!("line {line}: {name} {rate} is not finite"));
                }
            }
            for (name, d) in [
                ("long_rate_date", row.long_rate_date),
                ("short_rate_date", row.short_rate_date),
            ] {
                if d > row.date {
                    return Err(format!(
                        "line {line}: {name} {d} is after the row's date {}",
                        row.date
                    ));
                }
            }
            if let Some(prev) = rows.last() {
                if row.date <= prev.date {
                    return Err(format!(
                        "line {line}: date {} is not after the previous row's {}",
                        row.date, prev.date
                    ));
                }
            }
            rows.push(row);
        }
        if rows.is_empty() {
            return Err("the table has no rows".to_owned());
        }
        Ok(Self {
            sha256: found,
            rows,
        })
    }

    /// The file's sha256, lower-case hex.
    #[must_use]
    pub fn sha256(&self) -> &str {
        &self.sha256
    }

    /// Every row, by date.
    #[must_use]
    pub fn rows(&self) -> &[CarryRow] {
        &self.rows
    }

    /// The row for venue-local trading date `date`, if the table has one.
    #[must_use]
    pub fn row(&self, date: NaiveDate) -> Option<&CarryRow> {
        self.rows
            .binary_search_by_key(&date, |r| r.date)
            .ok()
            .map(|i| &self.rows[i])
    }
}

/// The sha256 of `bytes`, lower-case hex.
pub(crate) fn sha256_hex(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    Sha256::digest(bytes)
        .iter()
        .fold(String::new(), |mut s, b| {
            let _ = write!(s, "{b:02x}");
            s
        })
}

/// The dividend adjustment on a position held over an ex-dividend date: a long is
/// credited, a short is debited.
///
/// Optional: a venue × instrument without it charges nothing. Four forms are valid.
/// **Excluded** (`excluded = true` with a `reason`): nothing is charged, and costed
/// results omit it. **Estimated** (`estimated = true`): a dated ex-date table
/// ([`DividendTable`]) pinned by `table` and `sha256`, charged at the instant that `tz`,
/// `time` and `weekdays_before` set. **Verified** or **placeholder**: with the same five
/// table fields it charges from its table; with none of them it is a stated zero and
/// charges nothing. The table's sha256 is checked when the config loads.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Dividend {
    /// Where the venue states the adjustment, or the inputs of the table.
    pub source: String,
    /// `true` when the figures are an assumption or stand-in.
    pub placeholder: bool,
    /// `true` when the adjustment is not modelled: nothing is charged.
    #[serde(default)]
    pub excluded: bool,
    /// Why it is excluded and what costed results therefore omit.
    #[serde(default)]
    pub reason: Option<String>,
    /// `true` when the table is an estimate. Still costed, and listed by
    /// [`CostConfig::estimates`].
    #[serde(default)]
    pub estimated: bool,
    /// The ex-date table's CSV file. A relative path resolves against the cost file's
    /// directory (see [`CostConfig::from_toml_str_at`]).
    #[serde(default)]
    pub table: Option<PathBuf>,
    /// The sha256 of the table file, 64 hex digits (either case).
    #[serde(default)]
    pub sha256: Option<String>,
    /// The IANA zone of the charge instant, e.g. the index's exchange.
    #[serde(default, deserialize_with = "de_opt_tz")]
    pub tz: Option<Tz>,
    /// The local wall-clock time of the charge instant, `"HH:MM"` (for example the
    /// cash close).
    #[serde(default, deserialize_with = "de_opt_time")]
    pub time: Option<NaiveTime>,
    /// The weekday of the charge instant, counted back from the ex-date in weekdays
    /// (Monday to Friday): 0 is the ex-date itself, 1 the weekday before it. At most 5.
    #[serde(default)]
    pub weekdays_before: Option<u32>,
    /// The table, read and checked at load. `None` when the dividend charges nothing.
    #[serde(skip)]
    pub events: Option<Arc<DividendTable>>,
}

impl Dividend {
    /// Whether the dividend charges from a loaded ex-date table.
    #[must_use]
    pub fn is_charged(&self) -> bool {
        self.events.is_some()
    }
}

/// The CSV header a dividend table must have, in this order.
pub const DIVIDEND_TABLE_HEADER: [&str; 5] =
    ["date", "points", "long_share", "short_share", "bound"];

/// One row of a dividend table: the dividend for ex-date `date`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DividendRow {
    /// The ex-dividend date.
    pub date: NaiveDate,
    /// The dividend in index points (`pip_size` in quote price); not negative. A row with
    /// 0 states a covered date with no dividend.
    pub points: f64,
    /// The share of `points` credited to a long; not negative.
    pub long_share: f64,
    /// The share of `points` debited to a short; not negative.
    pub short_share: f64,
    /// `true` when `points` is a bound, not a figure for the date.
    pub bound: bool,
}

/// A dividend table: one row per ex-date, ascending. It covers its first row's date to
/// its last row's date.
#[derive(Debug, Clone, PartialEq)]
pub struct DividendTable {
    sha256: String,
    rows: Vec<DividendRow>,
}

impl DividendTable {
    /// Parse a table from its file bytes and check them against `sha256` (lower-case or
    /// upper-case hex).
    ///
    /// # Errors
    /// A message for a sha256 mismatch, a header other than [`DIVIDEND_TABLE_HEADER`], a
    /// row that does not parse, points or a share that is negative or not finite, a date
    /// that is not after the row before it, or a table with no rows.
    pub fn from_bytes(bytes: &[u8], sha256: &str) -> Result<Self, String> {
        let found = sha256_hex(bytes);
        if !found.eq_ignore_ascii_case(sha256) {
            return Err(format!("sha256 is {found}, but the config says {sha256}"));
        }
        let mut reader = csv::ReaderBuilder::new()
            .has_headers(true)
            .trim(csv::Trim::All)
            .from_reader(bytes);
        let header = reader.headers().map_err(|e| format!("header: {e}"))?;
        if header.iter().ne(DIVIDEND_TABLE_HEADER) {
            return Err(format!(
                "header is {:?}, expected {:?}",
                header.iter().collect::<Vec<_>>().join(","),
                DIVIDEND_TABLE_HEADER.join(",")
            ));
        }
        let mut rows: Vec<DividendRow> = Vec::new();
        for (i, record) in reader.deserialize::<DividendRow>().enumerate() {
            // Line 1 is the header.
            let line = i + 2;
            let row = record.map_err(|e| format!("line {line}: {e}"))?;
            for (name, v) in [
                ("points", row.points),
                ("long_share", row.long_share),
                ("short_share", row.short_share),
            ] {
                if !v.is_finite() || v < 0.0 {
                    return Err(format!(
                        "line {line}: {name} {v} is not a finite number of at least 0"
                    ));
                }
            }
            if let Some(prev) = rows.last() {
                if row.date <= prev.date {
                    return Err(format!(
                        "line {line}: date {} is not after the previous row's {}",
                        row.date, prev.date
                    ));
                }
            }
            rows.push(row);
        }
        if rows.is_empty() {
            return Err("the table has no rows".to_owned());
        }
        Ok(Self {
            sha256: found,
            rows,
        })
    }

    /// The file's sha256, lower-case hex.
    #[must_use]
    pub fn sha256(&self) -> &str {
        &self.sha256
    }

    /// Every row, by date.
    #[must_use]
    pub fn rows(&self) -> &[DividendRow] {
        &self.rows
    }

    /// The first date the table covers (its first row's).
    #[must_use]
    pub fn first_date(&self) -> NaiveDate {
        self.rows[0].date
    }

    /// The last date the table covers (its last row's).
    #[must_use]
    pub fn last_date(&self) -> NaiveDate {
        self.rows[self.rows.len() - 1].date
    }

    /// Whether `date` is in the table's first to last date.
    #[must_use]
    pub fn covers(&self, date: NaiveDate) -> bool {
        (self.first_date()..=self.last_date()).contains(&date)
    }

    /// The row for ex-date `date`, if the table has one.
    #[must_use]
    pub fn row(&self, date: NaiveDate) -> Option<&DividendRow> {
        self.rows
            .binary_search_by_key(&date, |r| r.date)
            .ok()
            .map(|i| &self.rows[i])
    }
}

/// Dealing costs and financing for one instrument at one venue. Each of the five cost
/// components carries its own `source`, `placeholder` flag and optional exclusion, and all
/// five are required; the spread excess and the dividend are optional.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VenueInstrumentCost {
    /// Price side used for marks and as the fill reference.
    pub price_side: PriceSide,
    /// Venue spread.
    pub spread: Spread,
    /// Per-fill slippage.
    pub slippage: Slippage,
    /// Commission (account currency).
    pub commission: Commission,
    /// Overnight financing: the venue's annual charge.
    pub financing: Financing,
    /// Overnight financing: the benchmark or tom-next carry.
    pub carry: Carry,
    /// The optional spread-excess charge. Absent: nothing is charged.
    #[serde(default)]
    pub spread_excess: Option<SpreadExcess>,
    /// The optional dividend adjustment on a position held over an ex-dividend date.
    /// Absent: nothing is charged.
    #[serde(default)]
    pub dividend: Option<Dividend>,
}

/// A cost component of a venue × instrument entry.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CostComponent {
    /// The venue spread.
    Spread,
    /// Per-fill slippage.
    Slippage,
    /// Commission.
    Commission,
    /// Overnight financing: the venue's annual charge.
    Financing,
    /// Overnight financing: the benchmark or tom-next carry.
    Carry,
    /// The optional spread-excess charge.
    SpreadExcess,
    /// The optional dividend adjustment.
    Dividend,
}

impl CostComponent {
    /// The config key, e.g. `"spread"`.
    #[must_use]
    pub fn key(self) -> &'static str {
        match self {
            Self::Spread => "spread",
            Self::Slippage => "slippage",
            Self::Commission => "commission",
            Self::Financing => "financing",
            Self::Carry => "carry",
            Self::SpreadExcess => "spread_excess",
            Self::Dividend => "dividend",
        }
    }

    /// What excluding this component does to a result. A spread or commission is the
    /// dealing cost a strategy's edge is measured against, so without it the venue ×
    /// instrument is [`ExclusionEffect::Uncosted`]. Slippage, the spread excess, the two
    /// financing parts and the dividend are [`ExclusionEffect::Omitted`]: the result stays
    /// costed and lists the omission.
    #[must_use]
    pub fn exclusion_effect(self) -> ExclusionEffect {
        match self {
            Self::Spread | Self::Commission => ExclusionEffect::Uncosted,
            Self::Slippage
            | Self::Financing
            | Self::Carry
            | Self::SpreadExcess
            | Self::Dividend => ExclusionEffect::Omitted,
        }
    }
}

/// Whether a component is a verified figure, a placeholder, excluded or estimated.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProvenanceStatus {
    /// A published venue figure with a dated source.
    Verified,
    /// An assumption or stand-in (`placeholder = true`).
    Placeholder,
    /// No published figure exists (`excluded = true`); the component charges nothing.
    Excluded,
    /// Estimated from data (`estimated = true`): costed, and listed with its source.
    Estimated,
}

/// What an excluded component does to a result.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExclusionEffect {
    /// The cost is not charged; the result is still costed and lists the omission.
    Omitted,
    /// The venue × instrument cannot be reported as costed.
    Uncosted,
}

/// One component's provenance.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ComponentProvenance<'a> {
    /// Which component.
    pub component: CostComponent,
    /// Verified, placeholder, excluded or estimated.
    pub status: ProvenanceStatus,
    /// The `source` note.
    pub source: &'a str,
    /// The exclusion `reason`, when there is one.
    pub reason: Option<&'a str>,
}

/// A component's three provenance flags. Validation makes at most one of them `true`.
#[derive(Debug, Clone, Copy)]
struct Flags {
    placeholder: bool,
    excluded: bool,
    estimated: bool,
}

impl Flags {
    fn count(self) -> usize {
        usize::from(self.placeholder) + usize::from(self.excluded) + usize::from(self.estimated)
    }
}

fn entry<'a>(
    component: CostComponent,
    flags: Flags,
    source: &'a str,
    reason: Option<&'a String>,
) -> ComponentProvenance<'a> {
    let status = if flags.excluded {
        ProvenanceStatus::Excluded
    } else if flags.placeholder {
        ProvenanceStatus::Placeholder
    } else if flags.estimated {
        ProvenanceStatus::Estimated
    } else {
        ProvenanceStatus::Verified
    };
    ComponentProvenance {
        component,
        status,
        source,
        reason: reason.map(String::as_str),
    }
}

impl VenueInstrumentCost {
    /// Each component's provenance and flags, in schema order: spread, slippage,
    /// commission, financing, carry, then the spread excess and the dividend when they are
    /// configured.
    fn flagged(&self) -> Vec<(ComponentProvenance<'_>, Flags)> {
        let plain = |placeholder, excluded| Flags {
            placeholder,
            excluded,
            estimated: false,
        };
        let mut out = vec![
            (
                CostComponent::Spread,
                plain(self.spread.placeholder, self.spread.excluded),
                self.spread.source.as_str(),
                self.spread.reason.as_ref(),
            ),
            (
                CostComponent::Slippage,
                Flags {
                    placeholder: self.slippage.placeholder,
                    excluded: self.slippage.excluded,
                    estimated: self.slippage.estimated,
                },
                self.slippage.source.as_str(),
                self.slippage.reason.as_ref(),
            ),
            (
                CostComponent::Commission,
                plain(self.commission.placeholder, self.commission.excluded),
                self.commission.source.as_str(),
                self.commission.reason.as_ref(),
            ),
            (
                CostComponent::Financing,
                plain(self.financing.placeholder, self.financing.excluded),
                self.financing.source.as_str(),
                self.financing.reason.as_ref(),
            ),
            (
                CostComponent::Carry,
                Flags {
                    placeholder: self.carry.placeholder,
                    excluded: self.carry.excluded,
                    estimated: self.carry.estimated,
                },
                self.carry.source.as_str(),
                self.carry.reason.as_ref(),
            ),
        ];
        if let Some(x) = &self.spread_excess {
            out.push((
                CostComponent::SpreadExcess,
                Flags {
                    placeholder: x.placeholder,
                    excluded: x.excluded,
                    estimated: x.estimated,
                },
                x.source.as_str(),
                x.reason.as_ref(),
            ));
        }
        if let Some(d) = &self.dividend {
            out.push((
                CostComponent::Dividend,
                Flags {
                    placeholder: d.placeholder,
                    excluded: d.excluded,
                    estimated: d.estimated,
                },
                d.source.as_str(),
                d.reason.as_ref(),
            ));
        }
        out.into_iter()
            .map(|(c, f, source, reason)| (entry(c, f, source, reason), f))
            .collect()
    }

    /// Each component's provenance, in schema order: spread, slippage, commission,
    /// financing, carry, then the spread excess and the dividend when they are
    /// configured.
    #[must_use]
    pub fn provenance(&self) -> Vec<ComponentProvenance<'_>> {
        self.flagged().into_iter().map(|(p, _)| p).collect()
    }

    /// Whether a fill needs the instrument's one-minute cost data: the slippage is
    /// [`SlippageModel::Volatility`] or a spread excess is configured, and that
    /// component is not excluded.
    #[must_use]
    pub fn needs_minute_costs(&self) -> bool {
        (self.slippage.model == SlippageModel::Volatility && !self.slippage.excluded)
            || self.spread_excess.as_ref().is_some_and(|x| !x.excluded)
    }

    /// Whether the entry uses a cost model beyond the fixed slippage: a
    /// [`SlippageModel::Volatility`] slippage or a `spread_excess` block (excluded or
    /// not). Fills then carry their cost detail.
    #[must_use]
    pub fn uses_extended_models(&self) -> bool {
        self.slippage.model == SlippageModel::Volatility || self.spread_excess.is_some()
    }

    /// Whether a result on this venue × instrument may be reported as costed: no
    /// component is a placeholder, and no excluded component is
    /// [`ExclusionEffect::Uncosted`].
    #[must_use]
    pub fn is_costed(&self) -> bool {
        self.provenance().iter().all(|p| match p.status {
            ProvenanceStatus::Verified | ProvenanceStatus::Estimated => true,
            ProvenanceStatus::Placeholder => false,
            ProvenanceStatus::Excluded => {
                p.component.exclusion_effect() == ExclusionEffect::Omitted
            }
        })
    }
}

/// One venue: its rollover calendar and its per-instrument costs.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Venue {
    /// Human-readable name.
    pub name: String,
    /// Spread bet or CFD.
    pub product: Product,
    /// Daily rollover instant.
    pub rollover: RolloverSpec,
    /// Weekend charging per asset class.
    pub weekend: WeekendRules,
    /// Costs keyed by instrument symbol.
    pub instruments: BTreeMap<String, VenueInstrumentCost>,
}

/// A configured figure that is not a verified venue number. Each run records the list
/// of placeholders for its venue in its metadata.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Placeholder {
    /// Venue id.
    pub venue: String,
    /// Instrument symbol.
    pub instrument: String,
    /// Which component is unverified.
    pub component: CostComponent,
    /// The provenance note from the config.
    pub source: String,
}

/// A component with no published figure, which charges nothing. Each run records the
/// list of exclusions for its venue in its metadata.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Exclusion {
    /// Venue id.
    pub venue: String,
    /// Instrument symbol.
    pub instrument: String,
    /// Which component is excluded.
    pub component: CostComponent,
    /// Whether results stay costed with the cost omitted, or are uncosted.
    pub effect: ExclusionEffect,
    /// The provenance note from the config: where the venue was checked.
    pub source: String,
    /// Why it is excluded and what results therefore omit.
    pub reason: String,
}

/// A component estimated from data (`estimated = true`): costed, and listed with its
/// source in each run's metadata.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Estimate {
    /// Venue id.
    pub venue: String,
    /// Instrument symbol.
    pub instrument: String,
    /// Which component is estimated.
    pub component: CostComponent,
    /// The provenance note from the config: the inputs of the estimate.
    pub source: String,
}

/// A modelled carry's table, recorded in run metadata.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CarryTableRef {
    /// The `table` path as configured.
    pub path: String,
    /// The file's sha256, lower-case hex, as checked at load.
    pub sha256: String,
    /// The carry's day-count divisor.
    pub day_count: u32,
    /// Rows in the table.
    pub rows: usize,
    /// The first row's date.
    pub first_date: NaiveDate,
    /// The last row's date.
    pub last_date: NaiveDate,
}

/// A charged dividend's ex-date table and charge instant, recorded in run metadata.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DividendTableRef {
    /// The `table` path as configured.
    pub path: String,
    /// The file's sha256, lower-case hex, as checked at load.
    pub sha256: String,
    /// Rows in the table.
    pub rows: usize,
    /// The first date the table covers (its first row's).
    pub first_date: NaiveDate,
    /// The last date the table covers (its last row's).
    pub last_date: NaiveDate,
    /// The IANA zone of the charge instant.
    pub tz: String,
    /// The local time of the charge instant.
    pub time: NaiveTime,
    /// Weekdays from the charge instant's day to the ex-date.
    pub weekdays_before: u32,
}

/// Whether a venue × instrument configures a spread excess.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SpreadExcessState {
    /// A `spread_excess` block is configured (it may be excluded; the provenance lists say).
    Present,
    /// No `spread_excess` block: nothing is charged.
    Absent,
}

/// The cost models one venue × instrument uses, recorded in run metadata.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct CostModels {
    /// The slippage model.
    pub slippage: SlippageModel,
    /// Whether a spread excess is configured.
    pub spread_excess: SpreadExcessState,
}

/// The whole cost configuration: instrument specs and venues, validated.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CostConfig {
    instruments: BTreeMap<String, InstrumentSpec>,
    venues: BTreeMap<String, Venue>,
}

impl CostConfig {
    /// The checked-in example, `crates/tradedesk-backtest/config/costs.toml`: illustrative
    /// figures, not any venue's.
    ///
    /// # Panics
    /// If the checked-in file fails to load. A unit test loads it, so this cannot ship.
    #[must_use]
    pub fn builtin() -> Self {
        Self::from_toml_str(BUILTIN_COSTS_TOML).expect("the checked-in costs.toml is valid")
    }

    /// Parse and validate a configuration. A modelled carry's relative `table` path
    /// resolves against the current directory.
    ///
    /// # Errors
    /// As [`CostConfig::from_toml_str_at`].
    pub fn from_toml_str(text: &str) -> Result<Self, ConfigError> {
        Self::from_toml_str_at(text, Path::new(""))
    }

    /// Parse and validate a configuration whose modelled carry tables, when their path is
    /// relative, are in or under `dir` (the cost file's own directory). Each table is
    /// read and checked against its `sha256` here, so a config in hand charges from the
    /// bytes it was configured with.
    ///
    /// # Errors
    /// [`ConfigError::Parse`] when the TOML does not match the schema;
    /// [`ConfigError::Invalid`] when a value is out of range or references an undefined
    /// instrument, or a carry table does not match its sha256 or does not parse; and
    /// [`ConfigError::Io`] when a carry table cannot be read.
    pub fn from_toml_str_at(text: &str, dir: &Path) -> Result<Self, ConfigError> {
        let mut config: Self = toml::from_str(text)?;
        config.validate()?;
        config.load_carry_tables(dir)?;
        config.load_dividend_tables(dir)?;
        Ok(config)
    }

    /// Read, parse and validate a configuration file. A modelled carry's relative
    /// `table` path resolves against the file's directory.
    ///
    /// # Errors
    /// [`ConfigError::Io`] when the file cannot be read; otherwise as
    /// [`CostConfig::from_toml_str_at`].
    pub fn from_path(path: impl AsRef<Path>) -> Result<Self, ConfigError> {
        let path = path.as_ref();
        let text = std::fs::read_to_string(path).map_err(|source| ConfigError::Io {
            path: path.to_path_buf(),
            source,
        })?;
        Self::from_toml_str_at(&text, path.parent().unwrap_or(Path::new("")))
    }

    /// Read and check every modelled carry's table (validation has checked its fields).
    fn load_carry_tables(&mut self, dir: &Path) -> Result<(), ConfigError> {
        for (id, venue) in &mut self.venues {
            for (symbol, cost) in &mut venue.instruments {
                let carry = &mut cost.carry;
                let (Some(table), Some(sha256)) = (&carry.table, &carry.sha256) else {
                    continue;
                };
                let path = dir.join(table);
                let bytes = std::fs::read(&path).map_err(|source| ConfigError::Io {
                    path: path.clone(),
                    source,
                })?;
                let rates = CarryTable::from_bytes(&bytes, sha256).map_err(|reason| {
                    invalid(
                        format!("venues.{id}.instruments.{symbol}.carry.table"),
                        format!("{}: {reason}", path.display()),
                    )
                })?;
                carry.rates = Some(Arc::new(rates));
            }
        }
        Ok(())
    }

    /// Read and check every dividend's ex-date table (validation has checked its fields).
    fn load_dividend_tables(&mut self, dir: &Path) -> Result<(), ConfigError> {
        for (id, venue) in &mut self.venues {
            for (symbol, cost) in &mut venue.instruments {
                let Some(dividend) = &mut cost.dividend else {
                    continue;
                };
                let (Some(table), Some(sha256)) = (&dividend.table, &dividend.sha256) else {
                    continue;
                };
                let path = dir.join(table);
                let bytes = std::fs::read(&path).map_err(|source| ConfigError::Io {
                    path: path.clone(),
                    source,
                })?;
                let events = DividendTable::from_bytes(&bytes, sha256).map_err(|reason| {
                    invalid(
                        format!("venues.{id}.instruments.{symbol}.dividend.table"),
                        format!("{}: {reason}", path.display()),
                    )
                })?;
                dividend.events = Some(Arc::new(events));
            }
        }
        Ok(())
    }

    /// For each instrument at `venue_id` whose dividend charges from a table, the table
    /// and the charge instant: the path as configured, the sha256, the row count, the
    /// first and last date, and the zone, time and weekdays before. Empty when no
    /// dividend there charges.
    ///
    /// # Errors
    /// [`ConfigError::UnknownVenue`] when the venue is not defined.
    pub fn dividend_tables(
        &self,
        venue_id: &str,
    ) -> Result<BTreeMap<String, DividendTableRef>, ConfigError> {
        let venue = self.venue_or_err(venue_id)?;
        Ok(venue
            .instruments
            .iter()
            .filter_map(|(symbol, cost)| {
                let d = cost.dividend.as_ref()?;
                let events = d.events.as_ref()?;
                Some((
                    symbol.clone(),
                    DividendTableRef {
                        path: d
                            .table
                            .as_ref()
                            .map(|p| p.display().to_string())
                            .unwrap_or_default(),
                        sha256: events.sha256().to_owned(),
                        rows: events.rows().len(),
                        first_date: events.first_date(),
                        last_date: events.last_date(),
                        tz: d.tz.map(|tz| tz.name().to_owned()).unwrap_or_default(),
                        time: d.time.unwrap_or_default(),
                        weekdays_before: d.weekdays_before.unwrap_or_default(),
                    },
                ))
            })
            .collect())
    }

    /// For each instrument at `venue_id` with a modelled carry, its table: the path as
    /// configured, the sha256, the row count and the first and last date. Empty when
    /// every carry there is excluded.
    ///
    /// # Errors
    /// [`ConfigError::UnknownVenue`] when the venue is not defined.
    pub fn carry_tables(
        &self,
        venue_id: &str,
    ) -> Result<BTreeMap<String, CarryTableRef>, ConfigError> {
        let venue = self.venue_or_err(venue_id)?;
        Ok(venue
            .instruments
            .iter()
            .filter_map(|(symbol, cost)| {
                let rates = cost.carry.rates.as_ref()?;
                let (first, last) = (rates.rows().first()?, rates.rows().last()?);
                Some((
                    symbol.clone(),
                    CarryTableRef {
                        path: cost
                            .carry
                            .table
                            .as_ref()
                            .map(|p| p.display().to_string())
                            .unwrap_or_default(),
                        sha256: rates.sha256().to_owned(),
                        day_count: cost.carry.day_count.unwrap_or_default(),
                        rows: rates.rows().len(),
                        first_date: first.date,
                        last_date: last.date,
                    },
                ))
            })
            .collect())
    }

    /// The spec for `symbol`.
    #[must_use]
    pub fn instrument(&self, symbol: &str) -> Option<&InstrumentSpec> {
        self.instruments.get(symbol)
    }

    /// Every instrument, by symbol.
    pub fn instruments(&self) -> impl Iterator<Item = (&str, &InstrumentSpec)> {
        self.instruments.iter().map(|(k, v)| (k.as_str(), v))
    }

    /// The venue with id `id`.
    #[must_use]
    pub fn venue(&self, id: &str) -> Option<&Venue> {
        self.venues.get(id)
    }

    /// Every venue, by id.
    pub fn venues(&self) -> impl Iterator<Item = (&str, &Venue)> {
        self.venues.iter().map(|(k, v)| (k.as_str(), v))
    }

    /// Every unverified cost component configured for `venue_id`, in instrument order and
    /// then spread, slippage, commission, financing.
    ///
    /// # Errors
    /// [`ConfigError::UnknownVenue`] when the venue is not defined.
    pub fn placeholders(&self, venue_id: &str) -> Result<Vec<Placeholder>, ConfigError> {
        let venue = self.venue_or_err(venue_id)?;
        let mut out = Vec::new();
        for (symbol, cost) in &venue.instruments {
            for p in cost.provenance() {
                if p.status == ProvenanceStatus::Placeholder {
                    out.push(Placeholder {
                        venue: venue_id.to_owned(),
                        instrument: symbol.clone(),
                        component: p.component,
                        source: p.source.to_owned(),
                    });
                }
            }
        }
        Ok(out)
    }

    /// Every excluded cost component configured for `venue_id`, in the same order as
    /// [`CostConfig::placeholders`].
    ///
    /// # Errors
    /// [`ConfigError::UnknownVenue`] when the venue is not defined.
    pub fn exclusions(&self, venue_id: &str) -> Result<Vec<Exclusion>, ConfigError> {
        let venue = self.venue_or_err(venue_id)?;
        let mut out = Vec::new();
        for (symbol, cost) in &venue.instruments {
            for p in cost.provenance() {
                if p.status == ProvenanceStatus::Excluded {
                    out.push(Exclusion {
                        venue: venue_id.to_owned(),
                        instrument: symbol.clone(),
                        component: p.component,
                        effect: p.component.exclusion_effect(),
                        source: p.source.to_owned(),
                        reason: p.reason.unwrap_or_default().to_owned(),
                    });
                }
            }
        }
        Ok(out)
    }

    /// Every estimated cost component configured for `venue_id`, in the same order as
    /// [`CostConfig::placeholders`].
    ///
    /// # Errors
    /// [`ConfigError::UnknownVenue`] when the venue is not defined.
    pub fn estimates(&self, venue_id: &str) -> Result<Vec<Estimate>, ConfigError> {
        let venue = self.venue_or_err(venue_id)?;
        let mut out = Vec::new();
        for (symbol, cost) in &venue.instruments {
            for p in cost.provenance() {
                if p.status == ProvenanceStatus::Estimated {
                    out.push(Estimate {
                        venue: venue_id.to_owned(),
                        instrument: symbol.clone(),
                        component: p.component,
                        source: p.source.to_owned(),
                    });
                }
            }
        }
        Ok(out)
    }

    /// For each instrument at `venue_id`, its slippage model and whether it configures a
    /// spread excess; `None` when no instrument there uses either beyond the default
    /// (fixed slippage, no spread excess), so that such a run's metadata is unchanged.
    ///
    /// # Errors
    /// [`ConfigError::UnknownVenue`] when the venue is not defined.
    pub fn cost_models(
        &self,
        venue_id: &str,
    ) -> Result<Option<BTreeMap<String, CostModels>>, ConfigError> {
        let venue = self.venue_or_err(venue_id)?;
        if !venue
            .instruments
            .values()
            .any(VenueInstrumentCost::uses_extended_models)
        {
            return Ok(None);
        }
        Ok(Some(
            venue
                .instruments
                .iter()
                .map(|(s, c)| {
                    let spread_excess = if c.spread_excess.is_some() {
                        SpreadExcessState::Present
                    } else {
                        SpreadExcessState::Absent
                    };
                    (
                        s.clone(),
                        CostModels {
                            slippage: c.slippage.model,
                            spread_excess,
                        },
                    )
                })
                .collect(),
        ))
    }

    /// The instruments at `venue_id` whose spread is a published minimum
    /// ([`SpreadBasis::Minimum`]): a result costed on them is a lower bound on spread
    /// cost.
    ///
    /// # Errors
    /// [`ConfigError::UnknownVenue`] when the venue is not defined.
    pub fn minimum_spreads(&self, venue_id: &str) -> Result<Vec<String>, ConfigError> {
        let venue = self.venue_or_err(venue_id)?;
        Ok(venue
            .instruments
            .iter()
            .filter(|(_, c)| c.spread.basis == Some(SpreadBasis::Minimum))
            .map(|(s, _)| s.clone())
            .collect())
    }

    /// For each instrument at `venue_id`, whether a result on it may be reported as
    /// costed ([`VenueInstrumentCost::is_costed`]).
    ///
    /// # Errors
    /// [`ConfigError::UnknownVenue`] when the venue is not defined.
    pub fn costed(&self, venue_id: &str) -> Result<BTreeMap<String, bool>, ConfigError> {
        let venue = self.venue_or_err(venue_id)?;
        Ok(venue
            .instruments
            .iter()
            .map(|(s, c)| (s.clone(), c.is_costed()))
            .collect())
    }

    fn venue_or_err(&self, venue_id: &str) -> Result<&Venue, ConfigError> {
        self.venue(venue_id)
            .ok_or_else(|| ConfigError::UnknownVenue(venue_id.to_owned()))
    }

    fn validate(&self) -> Result<(), ConfigError> {
        for (symbol, spec) in &self.instruments {
            let at = |field: &str| format!("instruments.{symbol}.{field}");
            positive(spec.raw_scale, &at("raw_scale"))?;
            positive(spec.pip_size, &at("pip_size"))?;
            positive(spec.divergence_tolerance, &at("divergence_tolerance"))?;
            currency(&spec.quote_currency, &at("quote_currency"))?;
        }
        for (id, venue) in &self.venues {
            for (class, rule) in [
                ("fx", Some(venue.weekend.fx)),
                ("metal", Some(venue.weekend.metal)),
                ("index", Some(venue.weekend.index)),
                ("energy", venue.weekend.energy),
            ] {
                if rule.is_some_and(|r| r.days_on_triple == 0) {
                    return Err(invalid(
                        format!("venues.{id}.weekend.{class}.days_on_triple"),
                        "must be at least 1",
                    ));
                }
            }
            for (symbol, cost) in &venue.instruments {
                let at = |field: &str| format!("venues.{id}.instruments.{symbol}.{field}");
                if !self.instruments.contains_key(symbol) {
                    return Err(invalid(
                        at("").trim_end_matches('.').to_owned(),
                        "no [instruments] entry defines this symbol",
                    ));
                }
                let class = self.instruments[symbol].asset_class;
                if venue.weekend.for_class(class).is_none() {
                    return Err(invalid(
                        format!("venues.{id}.weekend.energy"),
                        format!("required: the venue costs energy instrument {symbol}"),
                    ));
                }
                validate_spread(&cost.spread, &at)?;
                non_negative(cost.slippage.points, &at("slippage.points"))?;
                non_negative(cost.slippage.bps, &at("slippage.bps"))?;
                validate_slippage_model(&cost.slippage, &at)?;
                if let Some(x) = &cost.spread_excess {
                    validate_spread_excess(x, &at)?;
                }
                non_negative(cost.commission.per_fill, &at("commission.per_fill"))?;
                non_negative(
                    cost.commission.per_unit_per_fill,
                    &at("commission.per_unit_per_fill"),
                )?;
                non_negative(
                    cost.commission.per_round_trip,
                    &at("commission.per_round_trip"),
                )?;
                finite(cost.financing.long_rate, &at("financing.long_rate"))?;
                finite(cost.financing.short_rate, &at("financing.short_rate"))?;
                if !matches!(cost.financing.day_count, 360 | 365) {
                    return Err(invalid(at("financing.day_count"), "must be 360 or 365"));
                }
                validate_provenance(cost, &at)?;
            }
        }
        Ok(())
    }
}

fn validate_spread(spread: &Spread, at: &impl Fn(&str) -> String) -> Result<(), ConfigError> {
    non_negative(spread.pips, &at("spread.pips"))?;
    if spread.excluded {
        if spread.pips != 0.0 || spread.basis.is_some() || !spread.schedule.is_empty() {
            return Err(invalid(
                at("spread"),
                "an excluded spread charges nothing: pips = 0, and no basis or schedule",
            ));
        }
    } else if spread.basis.is_none() {
        return Err(invalid(
            at("spread.basis"),
            "must say whether the figure is an \"average\" or a \"minimum\"",
        ));
    }
    if spread.schedule.is_empty() != spread.tz.is_none() {
        return Err(invalid(
            at("spread.tz"),
            "a schedule needs a time zone, and a time zone needs a schedule",
        ));
    }
    let mut covered = [false; 24 * 60];
    for (i, w) in spread.schedule.iter().enumerate() {
        let here = at(&format!("spread.schedule[{i}]"));
        non_negative(w.pips, &format!("{here}.pips"))?;
        if w.from == w.to {
            return Err(invalid(here, "from and to are equal: the window is empty"));
        }
        for (minute, slot) in covered.iter_mut().enumerate() {
            let minute = u32::try_from(minute).unwrap_or_default();
            let t = NaiveTime::from_hms_opt(minute / 60, minute % 60, 0).unwrap_or_default();
            if w.contains(t) {
                if *slot {
                    return Err(invalid(
                        here,
                        format!(
                            "overlaps an earlier window at {:02}:{:02}",
                            t.hour(),
                            t.minute()
                        ),
                    ));
                }
                *slot = true;
            }
        }
    }
    Ok(())
}

/// The model-specific slippage fields: §2.1 of task #195.
fn validate_slippage_model(
    slippage: &Slippage,
    at: &impl Fn(&str) -> String,
) -> Result<(), ConfigError> {
    let volatility_fields = [
        ("market", slippage.market.is_some()),
        ("stop", slippage.stop.is_some()),
        ("limit", slippage.limit.is_some()),
        ("floor_minutes", slippage.floor_minutes.is_some()),
        ("max_gap_minutes", slippage.max_gap_minutes.is_some()),
    ];
    match slippage.model {
        SlippageModel::Fixed => {
            if let Some((field, _)) = volatility_fields.iter().find(|(_, set)| *set) {
                return Err(invalid(
                    at(&format!("slippage.{field}")),
                    "only a volatility slippage (model = \"volatility\") takes this field",
                ));
            }
        }
        SlippageModel::Volatility => {
            for (field, value) in [("points", slippage.points), ("bps", slippage.bps)] {
                if value != 0.0 {
                    return Err(invalid(
                        at(&format!("slippage.{field}")),
                        "a volatility slippage has no fixed amount: absent or 0",
                    ));
                }
            }
            for (field, value) in [
                ("market", slippage.market),
                ("stop", slippage.stop),
                ("limit", slippage.limit),
            ] {
                let here = at(&format!("slippage.{field}"));
                let Some(v) = value else {
                    return Err(invalid(here, "required by a volatility slippage"));
                };
                non_negative(v, &here)?;
            }
            for (field, value) in [
                ("floor_minutes", slippage.floor_minutes),
                ("max_gap_minutes", slippage.max_gap_minutes),
            ] {
                if value == Some(0) {
                    return Err(invalid(
                        at(&format!("slippage.{field}")),
                        "must be at least 1",
                    ));
                }
            }
        }
    }
    Ok(())
}

/// A spread excess: §2.2 of task #195.
fn validate_spread_excess(
    x: &SpreadExcess,
    at: &impl Fn(&str) -> String,
) -> Result<(), ConfigError> {
    if x.baseline_weekdays.is_some_and(|n| n < 2) {
        return Err(invalid(
            at("spread_excess.baseline_weekdays"),
            "must be at least 2",
        ));
    }
    let here = at("spread_excess.cap");
    finite(x.cap, &here)?;
    if x.cap < 1.0 {
        return Err(invalid(here, format!("{} is below 1", x.cap)));
    }
    Ok(())
}

/// Every component: a non-empty source; at most one of placeholder, excluded and
/// estimated; a reason exactly when excluded; an excluded component's amounts all zero;
/// the carry excluded or modelled.
fn validate_provenance(
    cost: &VenueInstrumentCost,
    at: &impl Fn(&str) -> String,
) -> Result<(), ConfigError> {
    for (p, flags) in cost.flagged() {
        let key = p.component.key();
        let excluded = p.status == ProvenanceStatus::Excluded;
        if p.source.trim().is_empty() {
            return Err(invalid(
                at(&format!("{key}.source")),
                "must say where the figure comes from",
            ));
        }
        if flags.count() > 1 {
            return Err(invalid(
                at(key),
                "is verified, a placeholder, excluded or estimated, never two of them",
            ));
        }
        match (excluded, p.reason) {
            (true, None) => {
                return Err(invalid(
                    at(&format!("{key}.reason")),
                    "an excluded component must say why",
                ));
            }
            (true, Some(r)) if r.trim().is_empty() => {
                return Err(invalid(
                    at(&format!("{key}.reason")),
                    "an excluded component must say why",
                ));
            }
            (false, Some(_)) => {
                return Err(invalid(
                    at(&format!("{key}.reason")),
                    "only an excluded component has a reason",
                ));
            }
            _ => {}
        }
    }
    let zero = |amounts: &[f64]| amounts.iter().all(|a| *a == 0.0);
    let charges = [
        (
            cost.slippage.excluded,
            zero(&[
                cost.slippage.points,
                cost.slippage.bps,
                cost.slippage.market.unwrap_or_default(),
                cost.slippage.stop.unwrap_or_default(),
                cost.slippage.limit.unwrap_or_default(),
            ]),
            "slippage",
        ),
        (
            cost.commission.excluded,
            zero(&[
                cost.commission.per_fill,
                cost.commission.per_unit_per_fill,
                cost.commission.per_round_trip,
            ]),
            "commission",
        ),
        (
            cost.financing.excluded,
            zero(&[cost.financing.long_rate, cost.financing.short_rate]),
            "financing",
        ),
    ];
    for (excluded, is_zero, key) in charges {
        if excluded && !is_zero {
            return Err(invalid(
                at(key),
                "an excluded component charges nothing: every amount must be 0",
            ));
        }
    }
    validate_carry(&cost.carry, at)?;
    cost.dividend
        .as_ref()
        .map_or(Ok(()), |d| validate_dividend(d, at))
}

/// The dividend is excluded (and has no table field), estimated (with every table field),
/// or verified or placeholder (with every table field or none).
fn validate_dividend(d: &Dividend, at: &impl Fn(&str) -> String) -> Result<(), ConfigError> {
    let fields = [
        ("table", d.table.is_some()),
        ("sha256", d.sha256.is_some()),
        ("tz", d.tz.is_some()),
        ("time", d.time.is_some()),
        ("weekdays_before", d.weekdays_before.is_some()),
    ];
    let any = fields.iter().any(|(_, set)| *set);
    if d.excluded {
        if let Some((field, _)) = fields.iter().find(|(_, set)| *set) {
            return Err(invalid(
                at(&format!("dividend.{field}")),
                "an excluded dividend charges nothing: only a charged dividend takes this field",
            ));
        }
        return Ok(());
    }
    if !d.estimated && !any {
        // A verified or placeholder zero: it charges nothing.
        return Ok(());
    }
    for (field, set) in fields {
        if !set {
            return Err(invalid(
                at(&format!("dividend.{field}")),
                "required by a dividend charged from a table",
            ));
        }
    }
    if d.weekdays_before.is_some_and(|n| n > 5) {
        return Err(invalid(at("dividend.weekdays_before"), "must be at most 5"));
    }
    let sha = d.sha256.as_deref().unwrap_or_default();
    if sha.len() != 64 || !sha.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(invalid(
            at("dividend.sha256"),
            format!("{sha:?} is not 64 hex digits"),
        ));
    }
    Ok(())
}

/// The carry is excluded (and has none of the modelled fields) or modelled (`estimated`
/// with a `table`, a `sha256` and a `day_count`).
fn validate_carry(carry: &Carry, at: &impl Fn(&str) -> String) -> Result<(), ConfigError> {
    let modelled_fields = [
        ("table", carry.table.is_some()),
        ("sha256", carry.sha256.is_some()),
        ("day_count", carry.day_count.is_some()),
    ];
    if carry.excluded {
        if let Some((field, _)) = modelled_fields.iter().find(|(_, set)| *set) {
            return Err(invalid(
                at(&format!("carry.{field}")),
                "an excluded carry charges nothing: only a modelled carry takes this field",
            ));
        }
        return Ok(());
    }
    if !carry.estimated {
        return Err(invalid(
            at("carry"),
            "a carry is excluded with a reason, or modelled from a rate table with \
             estimated = true",
        ));
    }
    for (field, set) in modelled_fields {
        if !set {
            return Err(invalid(
                at(&format!("carry.{field}")),
                "required by a modelled carry",
            ));
        }
    }
    if !matches!(carry.day_count, Some(360 | 365)) {
        return Err(invalid(at("carry.day_count"), "must be 360 or 365"));
    }
    let sha = carry.sha256.as_deref().unwrap_or_default();
    if sha.len() != 64 || !sha.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(invalid(
            at("carry.sha256"),
            format!("{sha:?} is not 64 hex digits"),
        ));
    }
    Ok(())
}

fn invalid(at: impl Into<String>, reason: impl Into<String>) -> ConfigError {
    ConfigError::Invalid {
        at: at.into(),
        reason: reason.into(),
    }
}

fn finite(v: f64, at: &str) -> Result<(), ConfigError> {
    if v.is_finite() {
        Ok(())
    } else {
        Err(invalid(at, format!("{v} is not finite")))
    }
}

fn non_negative(v: f64, at: &str) -> Result<(), ConfigError> {
    finite(v, at)?;
    if v < 0.0 {
        return Err(invalid(at, format!("{v} is negative")));
    }
    Ok(())
}

fn positive(v: f64, at: &str) -> Result<(), ConfigError> {
    finite(v, at)?;
    if v <= 0.0 {
        return Err(invalid(at, format!("{v} must be greater than zero")));
    }
    Ok(())
}

fn currency(code: &str, at: &str) -> Result<(), ConfigError> {
    if code.len() == 3 && code.bytes().all(|b| b.is_ascii_uppercase()) {
        Ok(())
    } else {
        Err(invalid(
            at,
            format!("{code:?} is not a three-letter ISO code"),
        ))
    }
}

/// Fixed synthetic figures for unit-test arithmetic, set on the example venues: flat
/// spreads, fixed slippage, annual financing and the CFD's per-unit commission, all
/// flagged verified. The ledger and metrics tests pin their hand-computed P&L to these, so
/// a change to the example figures in `costs.toml` does not move them.
#[cfg(test)]
pub(crate) fn arithmetic_fixture() -> CostConfig {
    const FIGURES: [(&str, f64, f64, f64); 5] = [
        ("EURUSD", 0.6, 0.2, 0.015),
        ("GBPJPY", 1.5, 0.2, 0.015),
        ("XAUUSD", 30.0, 5.0, 0.015),
        ("USA500IDXUSD", 0.4, 0.1, 0.034),
        ("DEUIDXEUR", 1.2, 0.2, 0.034),
    ];
    const CFD_SPREADS: [(&str, f64); 5] = [
        ("EURUSD", 0.1),
        ("GBPJPY", 0.5),
        ("XAUUSD", 8.0),
        ("USA500IDXUSD", 0.4),
        ("DEUIDXEUR", 1.2),
    ];
    let mut c = CostConfig::builtin();
    for venue in [
        "example_spread_bet",
        "example_ny_spread_bet",
        "example_ny_cfd",
    ] {
        let v = c.venues.get_mut(venue).expect("builtin venue");
        for (symbol, spread, slippage, rate) in FIGURES {
            // New York indices: 2.5%; everything else as at the London venue.
            let rate = if venue != "example_spread_bet" && rate > 0.02 {
                0.025
            } else {
                rate
            };
            let spread = CFD_SPREADS
                .iter()
                .find(|(s, _)| venue == "example_ny_cfd" && *s == symbol)
                .map_or(spread, |(_, p)| *p);
            let cost = v.instruments.get_mut(symbol).expect("builtin instrument");
            cost.spread = Spread {
                pips: spread,
                basis: Some(SpreadBasis::Average),
                tz: None,
                schedule: Vec::new(),
                source: "fixture".to_owned(),
                placeholder: false,
                excluded: false,
                reason: None,
            };
            cost.slippage.points = slippage;
            cost.slippage.excluded = false;
            cost.slippage.reason = None;
            cost.financing.long_rate = rate;
            cost.financing.short_rate = rate;
            cost.financing.excluded = false;
            cost.financing.reason = None;
            if venue == "example_ny_cfd" && symbol != "USA500IDXUSD" && symbol != "DEUIDXEUR" {
                // 2.25 per 100,000 units (FX) or per 100 ounces (gold), per fill.
                cost.commission.per_unit_per_fill = if symbol == "XAUUSD" {
                    0.0225
                } else {
                    0.000_022_5
                };
            }
        }
    }
    c.validate().expect("the fixture is valid");
    c
}

#[cfg(test)]
impl CostConfig {
    /// This config with `symbol`'s carry at `venue` modelled from the synthetic table
    /// `csv`, as if loaded from a file `carry.csv` pinned by its sha256.
    pub(crate) fn with_modelled_carry(
        mut self,
        venue: &str,
        symbol: &str,
        csv: &str,
        day_count: u32,
    ) -> Self {
        let sha256 = sha256_hex(csv.as_bytes());
        let table = CarryTable::from_bytes(csv.as_bytes(), &sha256).expect("a valid table");
        let carry = &mut self
            .venues
            .get_mut(venue)
            .expect("venue")
            .instruments
            .get_mut(symbol)
            .expect("instrument")
            .carry;
        *carry = Carry {
            source: "synthetic test table".to_owned(),
            placeholder: false,
            excluded: false,
            reason: None,
            estimated: true,
            table: Some(PathBuf::from("carry.csv")),
            sha256: Some(sha256),
            day_count: Some(day_count),
            rates: Some(Arc::new(table)),
        };
        self.validate().expect("the config is valid");
        self
    }

    /// This config with `symbol`'s dividend at `venue` estimated from the synthetic table
    /// `csv`, as if loaded from a file `dividend.csv` pinned by its sha256, charged at
    /// `time` in `tz` on the weekday `weekdays_before` weekdays before each ex-date.
    pub(crate) fn with_estimated_dividend(
        mut self,
        venue: &str,
        symbol: &str,
        csv: &str,
        (tz, time, weekdays_before): (Tz, &str, u32),
    ) -> Self {
        let sha256 = sha256_hex(csv.as_bytes());
        let table = DividendTable::from_bytes(csv.as_bytes(), &sha256).expect("a valid table");
        self.venues
            .get_mut(venue)
            .expect("venue")
            .instruments
            .get_mut(symbol)
            .expect("instrument")
            .dividend = Some(Dividend {
            source: "synthetic test table".to_owned(),
            placeholder: false,
            excluded: false,
            reason: None,
            estimated: true,
            table: Some(PathBuf::from("dividend.csv")),
            sha256: Some(sha256),
            tz: Some(tz),
            time: Some(NaiveTime::parse_from_str(time, "%H:%M").expect("HH:MM")),
            weekdays_before: Some(weekdays_before),
            events: Some(Arc::new(table)),
        });
        self.validate().expect("the config is valid");
        self
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    const REQUIRED: [&str; 5] = ["XAUUSD", "USA500IDXUSD", "GBPJPY", "EURUSD", "DEUIDXEUR"];

    #[test]
    fn builtin_config_loads_and_defines_every_required_venue_and_instrument() {
        let c = CostConfig::builtin();
        for venue in [
            "example_spread_bet",
            "example_ny_spread_bet",
            "example_ny_cfd",
        ] {
            let v = c.venue(venue).unwrap_or_else(|| panic!("{venue} missing"));
            for symbol in REQUIRED {
                assert!(v.instruments.contains_key(symbol), "{venue} lacks {symbol}");
            }
        }
        for symbol in REQUIRED {
            assert!(c.instrument(symbol).is_some(), "{symbol} has no spec");
        }
    }

    #[test]
    fn builtin_raw_scales_match_the_cache_on_disk() {
        // Verified against the market-data cache on 2026-10-06.
        let c = CostConfig::builtin();
        let quote = |s: &str, raw: f64| c.instrument(s).unwrap().to_quote(raw);
        assert!((quote("EURUSD", 11036.6) - 1.10366).abs() < 1e-12);
        assert!((quote("GBPJPY", 17960.1) - 179.601).abs() < 1e-9);
        assert!((quote("XAUUSD", 206_362.5) - 2063.625).abs() < 1e-9);
        assert!((quote("USA500IDXUSD", 4774.361) - 4774.361).abs() < 1e-12);
    }

    const VENUES: [&str; 3] = [
        "example_spread_bet",
        "example_ny_spread_bet",
        "example_ny_cfd",
    ];

    #[test]
    fn builtin_is_a_labelled_example_with_no_placeholder() {
        // Every component is an example figure that says so, or excluded with a reason.
        let c = CostConfig::builtin();
        for venue in VENUES {
            assert!(c.placeholders(venue).unwrap().is_empty(), "{venue}");
            let v = c.venue(venue).unwrap();
            for symbol in REQUIRED {
                for p in v.instruments[symbol].provenance() {
                    let at = format!("{venue} {symbol} {:?}", p.component);
                    assert_ne!(p.status, ProvenanceStatus::Placeholder, "{at}");
                    assert!(p.source.starts_with("example"), "{at}: {}", p.source);
                    assert!(!p.source.contains("://"), "{at}: {}", p.source);
                    if p.status == ProvenanceStatus::Excluded {
                        assert!(p.reason.is_some_and(|r| !r.is_empty()), "{at}");
                    }
                }
            }
        }
    }

    #[test]
    fn builtin_slippage_and_carry_are_excluded_everywhere_and_one_spread_is_uncosted() {
        let c = CostConfig::builtin();
        for venue in VENUES {
            let excluded = c.exclusions(venue).unwrap();
            for symbol in REQUIRED {
                for component in [CostComponent::Slippage, CostComponent::Carry] {
                    assert!(
                        excluded
                            .iter()
                            .any(|e| e.instrument == symbol && e.component == component),
                        "{venue} {symbol} {component:?}"
                    );
                }
            }
            let uncosted: Vec<&str> = c
                .costed(venue)
                .unwrap()
                .into_iter()
                .filter(|(_, costed)| !costed)
                .map(|(s, _)| if s == "GBPJPY" { "GBPJPY" } else { "other" })
                .collect();
            let expect: &[&str] = if venue == "example_ny_spread_bet" {
                &["GBPJPY"]
            } else {
                &[]
            };
            assert_eq!(uncosted, expect, "{venue}");
        }
    }

    #[test]
    fn builtin_example_spreads_cover_average_minimum_and_schedule() {
        // FX averages, the indices by time of day, gold a minimum.
        let c = CostConfig::builtin();
        let v = c.venue("example_spread_bet").unwrap();
        let spread = |s: &str| &v.instruments[s].spread;
        assert_eq!(spread("EURUSD").pips, 1.0);
        assert_eq!(spread("GBPJPY").pips, 3.0);
        for s in ["EURUSD", "GBPJPY"] {
            assert_eq!(spread(s).basis, Some(SpreadBasis::Average));
            assert!(spread(s).schedule.is_empty());
        }
        assert_eq!(spread("XAUUSD").pips, 40.0);
        assert_eq!(spread("XAUUSD").basis, Some(SpreadBasis::Minimum));
        for s in ["USA500IDXUSD", "DEUIDXEUR"] {
            assert_eq!(spread(s).tz, Some(chrono_tz::Europe::London));
            assert!(!spread(s).schedule.is_empty());
            assert_eq!(spread(s).basis, Some(SpreadBasis::Minimum));
        }
        assert_eq!(
            c.minimum_spreads("example_spread_bet").unwrap(),
            ["DEUIDXEUR", "USA500IDXUSD", "XAUUSD"]
        );
    }

    #[test]
    fn builtin_rollovers_are_london_22_and_new_york_17() {
        let c = CostConfig::builtin();
        let london = c.venue("example_spread_bet").unwrap();
        assert_eq!(london.rollover.tz, chrono_tz::Europe::London);
        assert_eq!(
            london.rollover.time,
            NaiveTime::from_hms_opt(22, 0, 0).unwrap()
        );
        for id in ["example_ny_spread_bet", "example_ny_cfd"] {
            let ny = c.venue(id).unwrap();
            assert_eq!(ny.rollover.tz, chrono_tz::America::New_York);
            assert_eq!(ny.rollover.time, NaiveTime::from_hms_opt(17, 0, 0).unwrap());
        }
    }

    #[test]
    fn placeholders_are_listed_per_venue() {
        let text = minimal("", "", LONDON).replace(
            r#"slippage = { source = "test", placeholder = false, excluded = true, reason = "none published" }"#,
            r#"slippage = { points = 0.2, source = "assumed", placeholder = true }"#,
        );
        let c = CostConfig::from_toml_str(&text).unwrap();
        let listed = c.placeholders("v").unwrap();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].component, CostComponent::Slippage);
        assert_eq!(listed[0].source, "assumed");
        assert!(!c.costed("v").unwrap()["EURUSD"]);
        assert!(matches!(
            c.placeholders("nowhere"),
            Err(ConfigError::UnknownVenue(_))
        ));
        assert!(matches!(
            c.exclusions("nowhere"),
            Err(ConfigError::UnknownVenue(_))
        ));
    }

    fn minimal(instrument_extra: &str, cost_extra: &str, rollover: &str) -> String {
        format!(
            r#"
[instruments.EURUSD]
asset_class = "fx"
quote_currency = "USD"
raw_scale = 0.0001
pip_size = 0.0001
divergence_tolerance = 50.0
{instrument_extra}

[venues.v]
name = "V"
product = "cfd"
rollover = {rollover}
weekend.fx = {{ triple_day = "wednesday" }}
weekend.metal = {{ triple_day = "wednesday" }}
weekend.index = {{ triple_day = "friday" }}

[venues.v.instruments.EURUSD]
price_side = "mid"
spread = {{ pips = 1.0, basis = "average", source = "test", placeholder = false }}
slippage = {{ source = "test", placeholder = false, excluded = true, reason = "none published" }}
commission = {{ source = "test", placeholder = false }}
financing = {{ long_rate = 0.02, short_rate = 0.01, day_count = 360, source = "test", placeholder = false }}
carry = {{ source = "test", placeholder = false, excluded = true, reason = "not modelled" }}
{cost_extra}
"#
        )
    }

    const LONDON: &str = r#"{ tz = "Europe/London", time = "22:00" }"#;

    #[test]
    fn minimal_config_loads_with_defaults() {
        let c = CostConfig::from_toml_str(&minimal("", "", LONDON)).unwrap();
        let cost = &c.venue("v").unwrap().instruments["EURUSD"];
        let c0 = &cost.commission;
        assert_eq!(
            (c0.per_fill, c0.per_unit_per_fill, c0.per_round_trip),
            (0.0, 0.0, 0.0)
        );
        assert_eq!((cost.slippage.points, cost.slippage.bps), (0.0, 0.0));
        assert_eq!(c.venue("v").unwrap().weekend.fx.days_on_triple, 3);
    }

    #[test]
    fn rejects_unknown_fields_bad_zones_and_bad_times() {
        for text in [
            minimal("pip_value = 1.0", "", LONDON),
            minimal("", "spred = 1.0", LONDON),
            // A component without its provenance does not load.
            minimal("", "", LONDON).replace(
                r#"commission = { source = "test", placeholder = false }"#,
                "commission = { per_fill = 1.0 }",
            ),
            // Every component is required: a missing slippage block is not a zero.
            minimal("", "", LONDON).replace(
                "slippage = { source = \"test\", placeholder = false, excluded = true, \
                 reason = \"none published\" }\n",
                "",
            ),
            // So is the carry.
            minimal("", "", LONDON).replace(
                "carry = { source = \"test\", placeholder = false, excluded = true, \
                 reason = \"not modelled\" }\n",
                "",
            ),
            // The old flat fields are gone.
            minimal("", "slippage_points = 0.2", LONDON),
            minimal("", "", r#"{ tz = "Europe/Londres", time = "22:00" }"#),
            minimal("", "", r#"{ tz = "Europe/London", time = "10pm" }"#),
        ] {
            assert!(
                matches!(CostConfig::from_toml_str(&text), Err(ConfigError::Parse(_))),
                "{text}"
            );
        }
    }

    #[test]
    fn rejects_out_of_range_values() {
        let cases = [
            minimal("", "", LONDON).replace(
                r#"commission = { source = "test""#,
                r#"commission = { per_unit_per_fill = -1.0, source = "test""#,
            ),
            minimal("", "", LONDON).replace(
                r#"commission = { source = "test""#,
                r#"commission = { per_fill = -2.0, source = "test""#,
            ),
            minimal("", "", LONDON).replace(
                r#"basis = "average", source = "test""#,
                r#"basis = "average", source = " ""#,
            ),
            minimal("", "", LONDON).replace("day_count = 360", "day_count = 364"),
            minimal("", "", LONDON).replace("raw_scale = 0.0001", "raw_scale = 0.0"),
            minimal("", "", LONDON)
                .replace(r#"quote_currency = "USD""#, r#"quote_currency = "usd""#),
            minimal("", "", LONDON).replace(
                r#"weekend.index = { triple_day = "friday" }"#,
                r#"weekend.index = { triple_day = "friday", days_on_triple = 0 }"#,
            ),
            minimal("", "", LONDON).replace(
                "[venues.v.instruments.EURUSD]",
                "[venues.v.instruments.GBPUSD]",
            ),
        ];
        for text in cases {
            assert!(
                matches!(
                    CostConfig::from_toml_str(&text),
                    Err(ConfigError::Invalid { .. })
                ),
                "{text}"
            );
        }
    }

    fn invalid_at(text: &str) -> String {
        match CostConfig::from_toml_str(text) {
            Err(ConfigError::Invalid { at, .. }) => at,
            other => panic!("expected Invalid, got {other:?} for\n{text}"),
        }
    }

    const SLIPPAGE: &str = r#"slippage = { source = "test", placeholder = false, excluded = true, reason = "none published" }"#;
    const SPREAD: &str =
        r#"spread = { pips = 1.0, basis = "average", source = "test", placeholder = false }"#;

    #[test]
    fn the_excluded_state_is_validated() {
        let base = minimal("", "", LONDON);
        let with = |from: &str, to: &str| base.replace(from, to);
        let cases = [
            // Excluded and a placeholder at once.
            (
                with(
                    SLIPPAGE,
                    r#"slippage = { source = "t", placeholder = true, excluded = true, reason = "r" }"#,
                ),
                "slippage",
            ),
            // Excluded without a reason, or with a blank one.
            (
                with(
                    SLIPPAGE,
                    r#"slippage = { source = "t", placeholder = false, excluded = true }"#,
                ),
                "slippage.reason",
            ),
            (
                with(
                    SLIPPAGE,
                    r#"slippage = { source = "t", placeholder = false, excluded = true, reason = " " }"#,
                ),
                "slippage.reason",
            ),
            // A reason on a component that is not excluded.
            (
                with(
                    SLIPPAGE,
                    r#"slippage = { points = 0.1, source = "t", placeholder = false, reason = "r" }"#,
                ),
                "slippage.reason",
            ),
            // An excluded component still charging something.
            (
                with(
                    SLIPPAGE,
                    r#"slippage = { points = 0.1, source = "t", placeholder = false, excluded = true, reason = "r" }"#,
                ),
                "slippage",
            ),
            (
                with(
                    r#"commission = { source = "test", placeholder = false }"#,
                    r#"commission = { per_round_trip = 1.0, source = "t", placeholder = false, excluded = true, reason = "r" }"#,
                ),
                "commission",
            ),
            (
                with(
                    r#"financing = { long_rate = 0.02, short_rate = 0.01, day_count = 360, source = "test", placeholder = false }"#,
                    r#"financing = { long_rate = 0.02, short_rate = 0.0, day_count = 360, source = "t", placeholder = false, excluded = true, reason = "r" }"#,
                ),
                "financing",
            ),
            (
                with(
                    SPREAD,
                    r#"spread = { pips = 0.5, source = "t", placeholder = false, excluded = true, reason = "r" }"#,
                ),
                "spread",
            ),
            // An excluded spread has no basis.
            (
                with(
                    SPREAD,
                    r#"spread = { pips = 0.0, basis = "minimum", source = "t", placeholder = false, excluded = true, reason = "r" }"#,
                ),
                "spread",
            ),
        ];
        for (text, field) in cases {
            assert_eq!(
                invalid_at(&text),
                format!("venues.v.instruments.EURUSD.{field}"),
                "{text}"
            );
        }
    }

    #[test]
    fn the_carry_forms_are_validated() {
        let base = minimal("", "", LONDON);
        let with = |from: &str, to: &str| base.replace(from, to);
        let cases = [
            // A carry neither excluded nor modelled.
            (
                with(
                    CARRY_EXCLUDED,
                    r#"carry = { source = "test", placeholder = false, excluded = false }"#,
                ),
                "carry",
            ),
            // An excluded carry takes none of the modelled fields.
            (
                with(
                    CARRY_EXCLUDED,
                    r#"carry = { source = "test", placeholder = false, excluded = true, reason = "r", table = "t.csv" }"#,
                ),
                "carry.table",
            ),
            (
                with(
                    CARRY_EXCLUDED,
                    r#"carry = { source = "test", placeholder = false, excluded = true, reason = "r", day_count = 365 }"#,
                ),
                "carry.day_count",
            ),
            // A modelled carry needs its table, sha256 and day count.
            (
                with(
                    CARRY_EXCLUDED,
                    r#"carry = { source = "test", placeholder = false, estimated = true }"#,
                ),
                "carry.table",
            ),
            (
                with(
                    CARRY_EXCLUDED,
                    r#"carry = { source = "test", placeholder = false, estimated = true, table = "t.csv", day_count = 365 }"#,
                ),
                "carry.sha256",
            ),
            (
                with(
                    CARRY_EXCLUDED,
                    &modelled_carry("t.csv", &"0".repeat(64)).replace(", day_count = 365", ""),
                ),
                "carry.day_count",
            ),
            (
                with(
                    CARRY_EXCLUDED,
                    &modelled_carry("t.csv", &"0".repeat(64)).replace("365", "364"),
                ),
                "carry.day_count",
            ),
            (
                with(CARRY_EXCLUDED, &modelled_carry("t.csv", "not-hex")),
                "carry.sha256",
            ),
            // Estimated and a placeholder at once.
            (
                with(
                    CARRY_EXCLUDED,
                    &modelled_carry("t.csv", &"0".repeat(64))
                        .replace("placeholder = false", "placeholder = true"),
                ),
                "carry",
            ),
        ];
        for (text, field) in cases {
            assert_eq!(
                invalid_at(&text),
                format!("venues.v.instruments.EURUSD.{field}"),
                "{text}"
            );
        }
    }

    const CARRY_EXCLUDED: &str = r#"carry = { source = "test", placeholder = false, excluded = true, reason = "not modelled" }"#;

    /// A labelled synthetic carry table: test values, not any venue's or any benchmark's.
    const CARRY_CSV: &str = "\
date,long_rate,short_rate,long_rate_date,short_rate_date,flag
2024-01-16,0.05,-0.04,2024-01-16,2024-01-16,
2024-01-17,0.06,-0.03,2024-01-16,2024-01-17,stale
";

    fn modelled_carry(table: &str, sha256: &str) -> String {
        format!(
            r#"carry = {{ source = "synthetic test table", placeholder = false, estimated = true, table = "{table}", sha256 = "{sha256}", day_count = 365 }}"#
        )
    }

    /// `minimal` with EURUSD's carry modelled from `table` (path as configured) pinned by
    /// `sha256`.
    fn with_modelled_carry(table: &str, sha256: &str) -> String {
        minimal("", "", LONDON).replace(CARRY_EXCLUDED, &modelled_carry(table, sha256))
    }

    #[test]
    fn the_table_hash_is_sha256_in_lower_case_hex() {
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn a_modelled_carry_loads_its_table_beside_the_cost_file_and_is_estimated() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir(dir.path().join("carry")).unwrap();
        std::fs::write(dir.path().join("carry/eurusd.csv"), CARRY_CSV).unwrap();
        let sha = sha256_hex(CARRY_CSV.as_bytes());
        let path = dir.path().join("costs.toml");
        // Upper-case hex in the file is accepted; the record keeps lower case.
        std::fs::write(
            &path,
            with_modelled_carry("carry/eurusd.csv", &sha.to_uppercase()),
        )
        .unwrap();
        let c = CostConfig::from_path(&path).unwrap();
        let carry = &c.venue("v").unwrap().instruments["EURUSD"].carry;
        assert!(carry.is_modelled());
        let table = carry.rates.as_ref().unwrap();
        assert_eq!(table.sha256(), sha);
        assert_eq!(table.rows().len(), 2);
        let d = |day| NaiveDate::from_ymd_opt(2024, 1, day).unwrap();
        assert_eq!(
            table.row(d(17)),
            Some(&CarryRow {
                date: d(17),
                long_rate: 0.06,
                short_rate: -0.03,
                long_rate_date: d(16),
                short_rate_date: d(17),
                flag: "stale".to_owned(),
            })
        );
        assert_eq!(table.row(d(16)).unwrap().flag, "");
        assert_eq!(table.row(d(18)), None);
        // Provenance: estimated, costed, not an exclusion.
        let listed: Vec<_> = c
            .estimates("v")
            .unwrap()
            .into_iter()
            .map(|e| (e.component, e.source))
            .collect();
        assert_eq!(
            listed,
            [(CostComponent::Carry, "synthetic test table".to_owned())]
        );
        assert!(
            c.exclusions("v")
                .unwrap()
                .iter()
                .all(|e| e.component != CostComponent::Carry)
        );
        assert!(c.costed("v").unwrap()["EURUSD"]);
        assert_eq!(
            c.carry_tables("v").unwrap()["EURUSD"],
            CarryTableRef {
                path: "carry/eurusd.csv".to_owned(),
                sha256: sha.clone(),
                day_count: 365,
                rows: 2,
                first_date: d(16),
                last_date: d(17),
            }
        );
        // The same text with the directory given.
        let text = std::fs::read_to_string(&path).unwrap();
        assert_eq!(CostConfig::from_toml_str_at(&text, dir.path()).unwrap(), c);
        // Every carry excluded: no table is listed.
        let builtin = CostConfig::builtin();
        for (id, _) in builtin.venues() {
            assert!(builtin.carry_tables(id).unwrap().is_empty());
        }
    }

    #[test]
    fn a_table_that_is_missing_altered_or_malformed_does_not_load() {
        let dir = tempfile::tempdir().unwrap();
        let load = |csv: &str, sha: &str| {
            std::fs::write(dir.path().join("t.csv"), csv).unwrap();
            CostConfig::from_toml_str_at(&with_modelled_carry("t.csv", sha), dir.path())
        };
        let refused = |csv: &str, expect: &str| {
            let sha = sha256_hex(csv.as_bytes());
            match load(csv, &sha) {
                Err(ConfigError::Invalid { at, reason }) => {
                    assert_eq!(at, "venues.v.instruments.EURUSD.carry.table");
                    assert!(reason.contains(expect), "{reason} lacks {expect:?}");
                }
                other => panic!("expected Invalid for\n{csv}, got {other:?}"),
            }
        };
        let header = "date,long_rate,short_rate,long_rate_date,short_rate_date,flag\n";
        // One byte changed after the hash was taken.
        let sha = sha256_hex(CARRY_CSV.as_bytes());
        match load(&CARRY_CSV.replace("0.06", "0.07"), &sha) {
            Err(ConfigError::Invalid { reason, .. }) => {
                assert!(reason.contains("sha256"), "{reason}");
            }
            other => panic!("expected a sha256 mismatch, got {other:?}"),
        }
        refused(
            &CARRY_CSV.replace("long_rate,short_rate", "short_rate,long_rate"),
            "header",
        );
        refused(header, "no rows");
        refused(
            &format!(
                "{header}2024-01-16,0.05,-0.04,2024-01-16,2024-01-16,\n2024-01-16,0.05,-0.04,2024-01-16,2024-01-16,\n"
            ),
            "not after",
        );
        refused(
            &format!(
                "{header}2024-01-17,0.05,-0.04,2024-01-17,2024-01-17,\n2024-01-16,0.05,-0.04,2024-01-16,2024-01-16,\n"
            ),
            "not after",
        );
        refused(
            &format!("{header}2024-01-16,NaN,-0.04,2024-01-16,2024-01-16,\n"),
            "not finite",
        );
        refused(
            &format!("{header}2024-01-16,0.05,inf,2024-01-16,2024-01-16,\n"),
            "not finite",
        );
        // A rate published after the rollover it is charged at is a look-ahead.
        refused(
            &format!("{header}2024-01-16,0.05,-0.04,2024-01-16,2024-01-17,\n"),
            "after the row's date",
        );
        refused(
            &format!("{header}2024-01-16,0.05,-0.04,16/01/2024,2024-01-16,\n"),
            "line 2",
        );
        refused(
            &format!("{header}2024-01-16,0.05,-0.04,2024-01-16\n"),
            "line 2",
        );
        // A table that is not there.
        assert!(matches!(
            CostConfig::from_toml_str_at(&with_modelled_carry("absent.csv", &sha), dir.path()),
            Err(ConfigError::Io { .. })
        ));
    }

    #[test]
    fn an_excluded_spread_or_commission_is_uncosted_and_others_are_omitted() {
        let base = minimal("", "", LONDON);
        let c = CostConfig::from_toml_str(&base).unwrap();
        let e = c.exclusions("v").unwrap();
        let listed: Vec<(CostComponent, ExclusionEffect, &str)> = e
            .iter()
            .map(|x| (x.component, x.effect, x.reason.as_str()))
            .collect();
        assert_eq!(
            listed,
            [
                (
                    CostComponent::Slippage,
                    ExclusionEffect::Omitted,
                    "none published"
                ),
                (
                    CostComponent::Carry,
                    ExclusionEffect::Omitted,
                    "not modelled"
                ),
            ]
        );
        assert!(c.costed("v").unwrap()["EURUSD"]);
        let no_spread = base.replace(
            SPREAD,
            r#"spread = { pips = 0.0, source = "t", placeholder = false, excluded = true, reason = "none" }"#,
        );
        let c = CostConfig::from_toml_str(&no_spread).unwrap();
        assert!(!c.costed("v").unwrap()["EURUSD"]);
        assert_eq!(
            c.exclusions("v").unwrap()[0].effect,
            ExclusionEffect::Uncosted
        );
        assert!(c.minimum_spreads("v").unwrap().is_empty());
        let no_commission = base.replace(
            r#"commission = { source = "test", placeholder = false }"#,
            r#"commission = { source = "t", placeholder = false, excluded = true, reason = "none" }"#,
        );
        let c = CostConfig::from_toml_str(&no_commission).unwrap();
        assert!(!c.costed("v").unwrap()["EURUSD"]);
    }

    #[test]
    fn a_spread_needs_a_basis_and_a_schedule_is_validated() {
        let base = minimal("", "", LONDON);
        let sched = |extra: &str| {
            base.replace(
                SPREAD,
                &format!(
                    r#"spread = {{ pips = 1.0, basis = "minimum", source = "t", placeholder = false, {extra} }}"#
                ),
            )
        };
        let ok = sched(
            r#"tz = "Europe/Berlin", schedule = [{ from = "22:00", to = "02:00", pips = 3.0 }, { from = "09:00", to = "17:30", pips = 0.8 }]"#,
        );
        let c = CostConfig::from_toml_str(&ok).unwrap();
        let spread = &c.venue("v").unwrap().instruments["EURUSD"].spread;
        assert_eq!(spread.schedule.len(), 2);
        assert_eq!(c.minimum_spreads("v").unwrap(), ["EURUSD"]);
        let berlin = |h| Utc.with_ymd_and_hms(2024, 1, 16, h, 0, 0).unwrap();
        assert_eq!(spread.pips_at(berlin(8)), 0.8); // 09:00 CET
        assert_eq!(spread.pips_at(berlin(0)), 3.0); // 01:00 CET, wrapped
        assert_eq!(spread.pips_at(berlin(18)), 1.0); // 19:00 CET, outside
        let cases = [
            (
                base.replace(
                    SPREAD,
                    r#"spread = { pips = 1.0, source = "t", placeholder = false }"#,
                ),
                "spread.basis",
            ),
            (
                sched(r#"schedule = [{ from = "09:00", to = "17:00", pips = 1.0 }]"#),
                "spread.tz",
            ),
            (sched(r#"tz = "Europe/London""#), "spread.tz"),
            (
                sched(
                    r#"tz = "Europe/London", schedule = [{ from = "09:00", to = "09:00", pips = 1.0 }]"#,
                ),
                "spread.schedule[0]",
            ),
            (
                sched(
                    r#"tz = "Europe/London", schedule = [{ from = "22:00", to = "01:00", pips = 1.0 }, { from = "00:30", to = "07:00", pips = 2.0 }]"#,
                ),
                "spread.schedule[1]",
            ),
            (
                sched(
                    r#"tz = "Europe/London", schedule = [{ from = "09:00", to = "17:00", pips = -1.0 }]"#,
                ),
                "spread.schedule[0].pips",
            ),
        ];
        for (text, field) in cases {
            assert_eq!(
                invalid_at(&text),
                format!("venues.v.instruments.EURUSD.{field}"),
                "{text}"
            );
        }
        // A window time that is not HH:MM, and an unknown basis, do not parse.
        for text in [
            sched(
                r#"tz = "Europe/London", schedule = [{ from = "9am", to = "17:00", pips = 1.0 }]"#,
            ),
            base.replace(r#"basis = "average""#, r#"basis = "typical""#),
        ] {
            assert!(matches!(
                CostConfig::from_toml_str(&text),
                Err(ConfigError::Parse(_))
            ));
        }
    }

    #[test]
    fn rejects_a_saturday_triple_day() {
        let text = minimal("", "", LONDON).replace(
            r#"weekend.fx = { triple_day = "wednesday" }"#,
            r#"weekend.fx = { triple_day = "saturday" }"#,
        );
        assert!(matches!(
            CostConfig::from_toml_str(&text),
            Err(ConfigError::Parse(_))
        ));
    }

    /// `minimal` plus a labelled example energy instrument (`EXAMPLEOIL`, not a cache
    /// symbol) costed at venue `v`, with `weekend_energy` as the venue's energy rule line.
    fn with_energy(weekend_energy: &str) -> String {
        let instrument = r#"
[instruments.EXAMPLEOIL]
asset_class = "energy"
quote_currency = "USD"
raw_scale = 0.01
pip_size = 0.01
divergence_tolerance = 500.0"#;
        let cost = format!(
            r#"
[venues.v.instruments.EXAMPLEOIL]
price_side = "mid"
{SPREAD}
{SLIPPAGE}
commission = {{ source = "test", placeholder = false }}
financing = {{ long_rate = 0.03, short_rate = 0.01, day_count = 365, source = "test", placeholder = false }}
carry = {{ source = "test", placeholder = false, excluded = true, reason = "not modelled" }}"#
        );
        minimal(instrument, &cost, LONDON).replace(
            "weekend.index = { triple_day = \"friday\" }\n",
            &format!("weekend.index = {{ triple_day = \"friday\" }}\n{weekend_energy}\n"),
        )
    }

    #[test]
    fn an_energy_instrument_loads_with_its_venue_weekend_rule() {
        let text = with_energy(r#"weekend.energy = { triple_day = "friday", days_on_triple = 3 }"#);
        let c = CostConfig::from_toml_str(&text).unwrap();
        assert_eq!(
            c.instrument("EXAMPLEOIL").unwrap().asset_class,
            AssetClass::Energy
        );
        assert_eq!(
            c.venue("v").unwrap().weekend.for_class(AssetClass::Energy),
            Some(WeekendRule {
                triple_day: TripleDay::Friday,
                days_on_triple: 3
            })
        );
    }

    #[test]
    fn the_energy_weekend_rule_is_required_only_where_an_energy_instrument_is_costed() {
        // An existing config without the key and without energy instruments loads unchanged.
        let c = CostConfig::from_toml_str(&minimal("", "", LONDON)).unwrap();
        assert_eq!(c.venue("v").unwrap().weekend.energy, None);
        assert_eq!(
            c.venue("v").unwrap().weekend.for_class(AssetClass::Energy),
            None
        );
        // A venue that costs an energy instrument and has no rule for it does not load.
        assert_eq!(invalid_at(&with_energy("")), "venues.v.weekend.energy");
        // When present, the rule is validated as the other classes are.
        assert_eq!(
            invalid_at(&with_energy(
                r#"weekend.energy = { triple_day = "friday", days_on_triple = 0 }"#
            )),
            "venues.v.weekend.energy.days_on_triple"
        );
        assert_eq!(
            invalid_at(&minimal("", "", LONDON).replace(
                "weekend.index = { triple_day = \"friday\" }\n",
                "weekend.index = { triple_day = \"friday\" }\n\
                 weekend.energy = { triple_day = \"friday\", days_on_triple = 0 }\n",
            )),
            "venues.v.weekend.energy.days_on_triple"
        );
        for text in [
            with_energy(r#"weekend.energy = { triple_day = "saturday" }"#),
            with_energy(r#"weekend.energy = { triple_day = "friday", days = 3 }"#),
            with_energy(r#"weekend.commodity = { triple_day = "friday" }"#),
            with_energy(r#"weekend.energy = { triple_day = "friday" }"#)
                .replace(r#"asset_class = "energy""#, r#"asset_class = "commodity""#),
        ] {
            assert!(
                matches!(CostConfig::from_toml_str(&text), Err(ConfigError::Parse(_))),
                "{text}"
            );
        }
    }

    /// A synthetic volatility slippage (test values, not venue figures).
    const VOLATILITY: &str = r#"slippage = { model = "volatility", market = 0.25, stop = 0.5, limit = 0.0, source = "synthetic", placeholder = false, estimated = true }"#;
    /// A synthetic spread excess.
    const EXCESS: &str = r#"spread_excess = { cap = 4.0, source = "synthetic", placeholder = false, estimated = true }"#;

    #[test]
    fn a_volatility_slippage_and_a_spread_excess_load_with_their_defaults() {
        let text = minimal("", EXCESS, LONDON).replace(SLIPPAGE, VOLATILITY);
        let c = CostConfig::from_toml_str(&text).unwrap();
        let cost = &c.venue("v").unwrap().instruments["EURUSD"];
        assert_eq!(cost.slippage.model, SlippageModel::Volatility);
        assert_eq!(
            (
                cost.slippage.market,
                cost.slippage.stop,
                cost.slippage.limit
            ),
            (Some(0.25), Some(0.5), Some(0.0))
        );
        assert_eq!(cost.slippage.floor_minutes(), 30);
        assert_eq!(cost.slippage.max_gap_minutes(), 5);
        let x = cost.spread_excess.as_ref().unwrap();
        assert_eq!((x.baseline_weekdays(), x.cap), (20, 4.0));
        assert!(cost.needs_minute_costs());
        // The model is fixed by default, and a config without the new keys has no
        // spread excess and needs no minute data.
        let plain = CostConfig::from_toml_str(&minimal("", "", LONDON)).unwrap();
        let cost = &plain.venue("v").unwrap().instruments["EURUSD"];
        assert_eq!(cost.slippage.model, SlippageModel::Fixed);
        assert!(cost.spread_excess.is_none());
        assert!(!cost.needs_minute_costs() && !cost.uses_extended_models());
        assert_eq!(plain.cost_models("v").unwrap(), None);
    }

    #[test]
    fn the_new_models_reject_invalid_configs() {
        // §4.7 of task #195.
        let vol = |from: &str, to: &str| {
            minimal("", "", LONDON).replace(SLIPPAGE, &VOLATILITY.replace(from, to))
        };
        let excess = |from: &str, to: &str| minimal("", &EXCESS.replace(from, to), LONDON);
        let cases = [
            (vol("market = 0.25", "market = 0.25, points = 0.2"), "slippage.points"),
            (vol("market = 0.25", "market = 0.25, bps = 1.0"), "slippage.bps"),
            (vol("market = 0.25", "market = -0.25"), "slippage.market"),
            (vol("stop = 0.5", "stop = inf"), "slippage.stop"),
            (vol("limit = 0.0", "limit = nan"), "slippage.limit"),
            (vol("limit = 0.0, ", ""), "slippage.limit"),
            (vol("market = 0.25", "market = 0.25, floor_minutes = 0"), "slippage.floor_minutes"),
            (
                vol("market = 0.25", "market = 0.25, max_gap_minutes = 0"),
                "slippage.max_gap_minutes",
            ),
            (
                minimal("", "", LONDON).replace(
                    SLIPPAGE,
                    r#"slippage = { points = 0.2, stop = 0.5, source = "t", placeholder = false }"#,
                ),
                "slippage.stop",
            ),
            (
                minimal("", "", LONDON).replace(
                    SLIPPAGE,
                    r#"slippage = { model = "fixed", floor_minutes = 30, source = "t", placeholder = false }"#,
                ),
                "slippage.floor_minutes",
            ),
            (
                vol("placeholder = false, estimated = true", "placeholder = true, estimated = true"),
                "slippage",
            ),
            (excess("cap = 4.0", "cap = 0.5"), "spread_excess.cap"),
            (excess("cap = 4.0", "cap = nan"), "spread_excess.cap"),
            (
                excess("cap = 4.0", "cap = 4.0, baseline_weekdays = 1"),
                "spread_excess.baseline_weekdays",
            ),
            (
                excess("estimated = true", "estimated = true, excluded = true, reason = \"r\""),
                "spread_excess",
            ),
            (excess("source = \"synthetic\"", "source = \" \""), "spread_excess.source"),
            // An excluded volatility slippage charges nothing.
            (
                vol("estimated = true", "excluded = true, reason = \"r\""),
                "slippage",
            ),
        ];
        for (text, field) in cases {
            assert_eq!(
                invalid_at(&text),
                format!("venues.v.instruments.EURUSD.{field}"),
                "{text}"
            );
        }
        // An unknown model, a fractional minute count and a spread excess with no cap do
        // not parse.
        for text in [
            vol("model = \"volatility\"", "model = \"atr\""),
            vol("market = 0.25", "market = 0.25, floor_minutes = 2.5"),
            excess("cap = 4.0, ", ""),
        ] {
            assert!(
                matches!(CostConfig::from_toml_str(&text), Err(ConfigError::Parse(_))),
                "{text}"
            );
        }
    }

    #[test]
    fn an_estimated_component_is_costed_and_listed_by_estimates_only() {
        // §4.9 of task #195.
        let text = minimal("", EXCESS, LONDON).replace(SLIPPAGE, VOLATILITY);
        let c = CostConfig::from_toml_str(&text).unwrap();
        let p = c.venue("v").unwrap().instruments["EURUSD"].provenance();
        assert_eq!(p.len(), 6);
        assert_eq!(p[1].status, ProvenanceStatus::Estimated);
        assert_eq!(p[5].component, CostComponent::SpreadExcess);
        assert_eq!(p[5].status, ProvenanceStatus::Estimated);
        assert!(c.costed("v").unwrap()["EURUSD"]);
        let listed: Vec<(CostComponent, &str)> = c
            .estimates("v")
            .unwrap()
            .iter()
            .map(|e| (e.component, "synthetic"))
            .collect();
        assert_eq!(
            listed,
            [
                (CostComponent::Slippage, "synthetic"),
                (CostComponent::SpreadExcess, "synthetic")
            ]
        );
        assert!(
            c.estimates("v")
                .unwrap()
                .iter()
                .all(|e| e.source == "synthetic")
        );
        assert!(c.placeholders("v").unwrap().is_empty());
        assert!(
            c.exclusions("v")
                .unwrap()
                .iter()
                .all(|e| e.component == CostComponent::Carry)
        );
        assert_eq!(
            c.cost_models("v").unwrap().unwrap()["EURUSD"],
            CostModels {
                slippage: SlippageModel::Volatility,
                spread_excess: SpreadExcessState::Present,
            }
        );
        assert!(matches!(
            c.estimates("nowhere"),
            Err(ConfigError::UnknownVenue(_))
        ));
        // A placeholder spread excess makes the instrument uncosted, as any placeholder
        // does; an excluded one is omitted and stays costed.
        let placeholder = minimal(
            "",
            &EXCESS.replace(
                "placeholder = false, estimated = true",
                "placeholder = true",
            ),
            LONDON,
        );
        assert!(
            !CostConfig::from_toml_str(&placeholder)
                .unwrap()
                .costed("v")
                .unwrap()["EURUSD"]
        );
        let excluded = minimal(
            "",
            &EXCESS.replace("estimated = true", "excluded = true, reason = \"none\""),
            LONDON,
        );
        let c = CostConfig::from_toml_str(&excluded).unwrap();
        assert!(c.costed("v").unwrap()["EURUSD"]);
        assert_eq!(
            c.exclusions("v").unwrap()[2].effect,
            ExclusionEffect::Omitted
        );
        assert_eq!(
            c.cost_models("v").unwrap().unwrap()["EURUSD"].spread_excess,
            SpreadExcessState::Present
        );
        assert!(!c.venue("v").unwrap().instruments["EURUSD"].needs_minute_costs());
    }

    #[test]
    fn the_builtin_example_keeps_the_fixed_slippage_and_no_spread_excess() {
        let c = CostConfig::builtin();
        for venue in VENUES {
            assert_eq!(c.cost_models(venue).unwrap(), None, "{venue}");
            assert!(c.estimates(venue).unwrap().is_empty(), "{venue}");
        }
    }

    #[test]
    fn from_path_reports_the_missing_file() {
        let err = CostConfig::from_path("/nonexistent/costs.toml").unwrap_err();
        assert!(matches!(err, ConfigError::Io { .. }));
    }

    /// A labelled synthetic dividend table: test values, not any index's or any venue's.
    const DIVIDEND_CSV: &str = "\
date,points,long_share,short_share,bound
2024-01-02,0,1,1,false
2024-01-17,1.5,0.8,1,false
2024-01-31,2.25,0,1.1,true
";

    fn estimated_dividend(table: &str, sha256: &str) -> String {
        format!(
            r#"dividend = {{ source = "synthetic test table", placeholder = false, estimated = true, table = "{table}", sha256 = "{sha256}", tz = "America/New_York", time = "16:00", weekdays_before = 1 }}"#
        )
    }

    #[test]
    fn a_cost_file_without_a_dividend_loads_and_lists_nothing() {
        let c = CostConfig::from_toml_str(&minimal("", "", LONDON)).unwrap();
        let cost = &c.venue("v").unwrap().instruments["EURUSD"];
        assert!(cost.dividend.is_none());
        assert!(
            cost.provenance()
                .iter()
                .all(|p| p.component != CostComponent::Dividend)
        );
        assert!(c.dividend_tables("v").unwrap().is_empty());
        let builtin = CostConfig::builtin();
        for (id, venue) in builtin.venues() {
            assert!(builtin.dividend_tables(id).unwrap().is_empty());
            assert!(venue.instruments.values().all(|i| i.dividend.is_none()));
        }
    }

    #[test]
    fn an_estimated_dividend_loads_its_table_beside_the_cost_file() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir(dir.path().join("dividend")).unwrap();
        std::fs::write(dir.path().join("dividend/x.csv"), DIVIDEND_CSV).unwrap();
        let sha = sha256_hex(DIVIDEND_CSV.as_bytes());
        let path = dir.path().join("costs.toml");
        std::fs::write(
            &path,
            minimal(
                "",
                &estimated_dividend("dividend/x.csv", &sha.to_uppercase()),
                LONDON,
            ),
        )
        .unwrap();
        let c = CostConfig::from_path(&path).unwrap();
        let cost = &c.venue("v").unwrap().instruments["EURUSD"];
        let dividend = cost.dividend.as_ref().unwrap();
        assert!(dividend.is_charged());
        let table = dividend.events.as_ref().unwrap();
        assert_eq!(table.sha256(), sha);
        let d = |m, day| NaiveDate::from_ymd_opt(2024, m, day).unwrap();
        assert_eq!(table.rows().len(), 3);
        assert_eq!(
            table.row(d(1, 31)),
            Some(&DividendRow {
                date: d(1, 31),
                points: 2.25,
                long_share: 0.0,
                short_share: 1.1,
                bound: true,
            })
        );
        assert!(table.row(d(1, 16)).is_none());
        assert!(table.covers(d(1, 2)) && table.covers(d(1, 31)));
        assert!(!table.covers(d(1, 1)) && !table.covers(d(2, 1)));
        // Estimated: costed and listed, never excluded.
        assert_eq!(
            c.estimates("v")
                .unwrap()
                .iter()
                .filter(|e| e.component == CostComponent::Dividend)
                .map(|e| e.source.as_str())
                .collect::<Vec<_>>(),
            ["synthetic test table"]
        );
        assert!(c.costed("v").unwrap()["EURUSD"]);
        assert_eq!(
            c.dividend_tables("v").unwrap()["EURUSD"],
            DividendTableRef {
                path: "dividend/x.csv".to_owned(),
                sha256: sha,
                rows: 3,
                first_date: d(1, 2),
                last_date: d(1, 31),
                tz: "America/New_York".to_owned(),
                time: NaiveTime::from_hms_opt(16, 0, 0).unwrap(),
                weekdays_before: 1,
            }
        );
    }

    #[test]
    fn the_dividend_statuses_are_listed_and_an_excluded_one_is_omitted() {
        let load = |dividend: &str| CostConfig::from_toml_str(&minimal("", dividend, LONDON));
        // Verified zero: a stated fact, charges nothing, costed.
        let c = load(
            r#"dividend = { source = "test: the venue makes no adjustment", placeholder = false }"#,
        )
        .unwrap();
        let d = c.venue("v").unwrap().instruments["EURUSD"]
            .dividend
            .clone()
            .unwrap();
        assert!(!d.is_charged());
        let p = c.venue("v").unwrap().instruments["EURUSD"].provenance();
        assert_eq!(p.last().unwrap().component, CostComponent::Dividend);
        assert_eq!(p.last().unwrap().status, ProvenanceStatus::Verified);
        assert!(c.costed("v").unwrap()["EURUSD"]);
        assert!(c.dividend_tables("v").unwrap().is_empty());
        // Placeholder: listed, and the instrument is not costed.
        let c = load(r#"dividend = { source = "test", placeholder = true }"#).unwrap();
        assert_eq!(
            c.placeholders("v").unwrap()[0].component,
            CostComponent::Dividend
        );
        assert!(!c.costed("v").unwrap()["EURUSD"]);
        // Excluded: omitted, so still costed.
        let c = load(r#"dividend = { source = "test", placeholder = false, excluded = true, reason = "not modelled" }"#)
            .unwrap();
        let e = c
            .exclusions("v")
            .unwrap()
            .into_iter()
            .find(|e| e.component == CostComponent::Dividend)
            .unwrap();
        assert_eq!(e.effect, ExclusionEffect::Omitted);
        assert_eq!(e.reason, "not modelled");
        assert!(c.costed("v").unwrap()["EURUSD"]);
        assert_eq!(CostComponent::Dividend.key(), "dividend");
    }

    #[test]
    fn the_dividend_forms_are_validated() {
        let sha = "0".repeat(64);
        let estimated = estimated_dividend("t.csv", &sha);
        let without = |field: &str| {
            let start = estimated.find(&format!(", {field} =")).unwrap();
            let rest = &estimated[start + 2..];
            let end = rest.find(", ").or_else(|| rest.find(" }")).unwrap();
            format!("{}{}", &estimated[..start], &rest[end..])
        };
        let cases = [
            // An estimated dividend needs every table field.
            (without("table"), "dividend.table"),
            (without("sha256"), "dividend.sha256"),
            (without("tz"), "dividend.tz"),
            (without("time"), "dividend.time"),
            (without("weekdays_before"), "dividend.weekdays_before"),
            (
                r#"dividend = { source = "test", placeholder = false, estimated = true }"#
                    .to_owned(),
                "dividend.table",
            ),
            // A verified dividend takes every table field or none.
            (
                r#"dividend = { source = "test", placeholder = false, table = "t.csv" }"#
                    .to_owned(),
                "dividend.sha256",
            ),
            // An excluded dividend takes none.
            (
                r#"dividend = { source = "test", placeholder = false, excluded = true, reason = "r", tz = "UTC" }"#
                    .to_owned(),
                "dividend.tz",
            ),
            (
                r#"dividend = { source = "test", placeholder = false, excluded = true }"#
                    .to_owned(),
                "dividend.reason",
            ),
            (
                r#"dividend = { source = "test", placeholder = false, reason = "r" }"#.to_owned(),
                "dividend.reason",
            ),
            (
                r#"dividend = { source = " ", placeholder = false }"#.to_owned(),
                "dividend.source",
            ),
            (
                r#"dividend = { source = "test", placeholder = true, excluded = true, reason = "r" }"#
                    .to_owned(),
                "dividend",
            ),
            (
                estimated.replace("weekdays_before = 1", "weekdays_before = 6"),
                "dividend.weekdays_before",
            ),
            (
                estimated.replace(&sha, "not-hex"),
                "dividend.sha256",
            ),
        ];
        for (dividend, expect) in cases {
            match CostConfig::from_toml_str(&minimal("", &dividend, LONDON)) {
                Err(ConfigError::Invalid { at, .. }) => {
                    assert_eq!(
                        at,
                        format!("venues.v.instruments.EURUSD.{expect}"),
                        "{dividend}"
                    );
                }
                other => panic!("expected Invalid at {expect} for {dividend}, got {other:?}"),
            }
        }
        // An unknown key, a bad zone and a bad time do not parse.
        for dividend in [
            estimated.replace("weekdays_before", "days_before"),
            estimated.replace("America/New_York", "America/Nowhere"),
            estimated.replace("16:00", "4pm"),
        ] {
            assert!(
                matches!(
                    CostConfig::from_toml_str(&minimal("", &dividend, LONDON)),
                    Err(ConfigError::Parse(_))
                ),
                "{dividend}"
            );
        }
    }

    #[test]
    fn a_dividend_table_that_is_missing_altered_or_malformed_does_not_load() {
        let dir = tempfile::tempdir().unwrap();
        let load = |csv: &str, sha: &str| {
            std::fs::write(dir.path().join("t.csv"), csv).unwrap();
            CostConfig::from_toml_str_at(
                &minimal("", &estimated_dividend("t.csv", sha), LONDON),
                dir.path(),
            )
        };
        let refused = |csv: &str, expect: &str| {
            let sha = sha256_hex(csv.as_bytes());
            match load(csv, &sha) {
                Err(ConfigError::Invalid { at, reason }) => {
                    assert_eq!(at, "venues.v.instruments.EURUSD.dividend.table");
                    assert!(reason.contains(expect), "{reason} lacks {expect:?}");
                }
                other => panic!("expected Invalid for\n{csv}, got {other:?}"),
            }
        };
        let header = "date,points,long_share,short_share,bound\n";
        let sha = sha256_hex(DIVIDEND_CSV.as_bytes());
        match load(&DIVIDEND_CSV.replace("1.5", "1.6"), &sha) {
            Err(ConfigError::Invalid { reason, .. }) => assert!(reason.contains("sha256")),
            other => panic!("expected a sha256 mismatch, got {other:?}"),
        }
        refused(
            &DIVIDEND_CSV.replace("long_share,short_share", "short_share,long_share"),
            "header",
        );
        refused(header, "no rows");
        refused(
            &format!("{header}2024-01-17,1,1,1,false\n2024-01-17,1,1,1,false\n"),
            "not after",
        );
        refused(
            &format!("{header}2024-01-17,1,1,1,false\n2024-01-16,1,1,1,false\n"),
            "not after",
        );
        refused(&format!("{header}2024-01-17,-1,1,1,false\n"), "points -1");
        refused(&format!("{header}2024-01-17,NaN,1,1,false\n"), "points NaN");
        refused(
            &format!("{header}2024-01-17,1,-0.5,1,false\n"),
            "long_share",
        );
        refused(
            &format!("{header}2024-01-17,1,1,inf,false\n"),
            "short_share",
        );
        refused(&format!("{header}2024-01-17,1,1,1,maybe\n"), "line 2");
        refused(&format!("{header}17/01/2024,1,1,1,false\n"), "line 2");
        assert!(matches!(
            CostConfig::from_toml_str_at(
                &minimal("", &estimated_dividend("absent.csv", &sha), LONDON),
                dir.path()
            ),
            Err(ConfigError::Io { .. })
        ));
    }
}
