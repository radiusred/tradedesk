//! The venue cost configuration: per-instrument units and per-venue, per-instrument costs.
//!
//! The checked-in defaults live in `crates/tradedesk-backtest/config/costs.toml` and load with
//! [`CostConfig::builtin`]. A run can load its own file with the same schema through
//! [`CostConfig::from_path`] or [`CostConfig::from_toml_str`]. Every load is validated
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
//! every venue × instrument each carry their own `source` and a status. A component is
//! **verified** (a published venue figure), a **placeholder** (`placeholder = true`: an
//! assumption or stand-in) or **excluded** (`excluded = true` with a `reason`: no published
//! figure exists, so the component charges nothing). [`CostConfig::placeholders`] and
//! [`CostConfig::exclusions`] list them, and [`CostConfig::costed`] says which instruments
//! can be reported as costed (see [`CostComponent::exclusion_effect`]).

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use chrono::{DateTime, NaiveTime, Timelike, Utc, Weekday};
use chrono_tz::Tz;
use serde::{Deserialize, Deserializer, Serialize};

/// The checked-in cost configuration (see the module docs).
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

/// The weekend rule for each asset class. All three are required.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WeekendRules {
    /// Spot FX.
    pub fx: WeekendRule,
    /// Spot metals.
    pub metal: WeekendRule,
    /// Indices.
    pub index: WeekendRule,
}

impl WeekendRules {
    /// The rule for `class`.
    #[must_use]
    pub fn for_class(&self, class: AssetClass) -> WeekendRule {
        match class {
            AssetClass::Fx => self.fx,
            AssetClass::Metal => self.metal,
            AssetClass::Index => self.index,
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

/// Adverse slippage applied on every fill.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Slippage {
    /// Fixed slippage per fill, in pips/points.
    #[serde(default)]
    pub points: f64,
    /// Proportional slippage per fill, in basis points of the mark.
    #[serde(default)]
    pub bps: f64,
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
/// a venue adds to or subtracts from its admin fee. It is not modelled (there is no rate
/// history in the config), so every entry must declare it `excluded` with a reason, and
/// costed results omit it.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Carry {
    /// Where the venue states how the carry is set.
    pub source: String,
    /// Must be `false`: an excluded component is not a placeholder.
    pub placeholder: bool,
    /// Must be `true` (see the type docs).
    pub excluded: bool,
    /// Why it is excluded and what costed results therefore omit.
    #[serde(default)]
    pub reason: Option<String>,
}

/// Dealing costs and financing for one instrument at one venue. Each of the five cost
/// components carries its own `source`, `placeholder` flag and optional exclusion, and all
/// five are required.
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
        }
    }

    /// What excluding this component does to a result. A spread or commission is the
    /// dealing cost a strategy's edge is measured against, so without it the venue ×
    /// instrument is [`ExclusionEffect::Uncosted`]. Slippage and the two financing parts
    /// are [`ExclusionEffect::Omitted`]: the result stays costed and lists the omission.
    #[must_use]
    pub fn exclusion_effect(self) -> ExclusionEffect {
        match self {
            Self::Spread | Self::Commission => ExclusionEffect::Uncosted,
            Self::Slippage | Self::Financing | Self::Carry => ExclusionEffect::Omitted,
        }
    }
}

/// Whether a component is a verified figure, a placeholder or excluded.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProvenanceStatus {
    /// A published venue figure with a dated source.
    Verified,
    /// An assumption or stand-in (`placeholder = true`).
    Placeholder,
    /// No published figure exists (`excluded = true`); the component charges nothing.
    Excluded,
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
    /// Verified, placeholder or excluded.
    pub status: ProvenanceStatus,
    /// The `source` note.
    pub source: &'a str,
    /// The exclusion `reason`, when there is one.
    pub reason: Option<&'a str>,
}

fn entry<'a>(
    component: CostComponent,
    placeholder: bool,
    excluded: bool,
    source: &'a str,
    reason: Option<&'a String>,
) -> ComponentProvenance<'a> {
    let status = if excluded {
        ProvenanceStatus::Excluded
    } else if placeholder {
        ProvenanceStatus::Placeholder
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
    /// Each component's provenance, in schema order: spread, slippage, commission,
    /// financing, carry.
    #[must_use]
    pub fn provenance(&self) -> [ComponentProvenance<'_>; 5] {
        [
            entry(
                CostComponent::Spread,
                self.spread.placeholder,
                self.spread.excluded,
                &self.spread.source,
                self.spread.reason.as_ref(),
            ),
            entry(
                CostComponent::Slippage,
                self.slippage.placeholder,
                self.slippage.excluded,
                &self.slippage.source,
                self.slippage.reason.as_ref(),
            ),
            entry(
                CostComponent::Commission,
                self.commission.placeholder,
                self.commission.excluded,
                &self.commission.source,
                self.commission.reason.as_ref(),
            ),
            entry(
                CostComponent::Financing,
                self.financing.placeholder,
                self.financing.excluded,
                &self.financing.source,
                self.financing.reason.as_ref(),
            ),
            entry(
                CostComponent::Carry,
                self.carry.placeholder,
                self.carry.excluded,
                &self.carry.source,
                self.carry.reason.as_ref(),
            ),
        ]
    }

    /// Whether a result on this venue × instrument may be reported as costed: no
    /// component is a placeholder, and no excluded component is
    /// [`ExclusionEffect::Uncosted`].
    #[must_use]
    pub fn is_costed(&self) -> bool {
        self.provenance().iter().all(|p| match p.status {
            ProvenanceStatus::Verified => true,
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

/// The whole cost configuration: instrument specs and venues, validated.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CostConfig {
    instruments: BTreeMap<String, InstrumentSpec>,
    venues: BTreeMap<String, Venue>,
}

impl CostConfig {
    /// The checked-in configuration, `crates/tradedesk-backtest/config/costs.toml`.
    ///
    /// # Panics
    /// If the checked-in file fails to load. A unit test loads it, so this cannot ship.
    #[must_use]
    pub fn builtin() -> Self {
        Self::from_toml_str(BUILTIN_COSTS_TOML).expect("the checked-in costs.toml is valid")
    }

    /// Parse and validate a configuration.
    ///
    /// # Errors
    /// [`ConfigError::Parse`] when the TOML does not match the schema, and
    /// [`ConfigError::Invalid`] when a value is out of range or references an undefined
    /// instrument.
    pub fn from_toml_str(text: &str) -> Result<Self, ConfigError> {
        let config: Self = toml::from_str(text)?;
        config.validate()?;
        Ok(config)
    }

    /// Read, parse and validate a configuration file.
    ///
    /// # Errors
    /// [`ConfigError::Io`] when the file cannot be read; otherwise as
    /// [`CostConfig::from_toml_str`].
    pub fn from_path(path: impl AsRef<Path>) -> Result<Self, ConfigError> {
        let path = path.as_ref();
        let text = std::fs::read_to_string(path).map_err(|source| ConfigError::Io {
            path: path.to_path_buf(),
            source,
        })?;
        Self::from_toml_str(&text)
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
                ("fx", venue.weekend.fx),
                ("metal", venue.weekend.metal),
                ("index", venue.weekend.index),
            ] {
                if rule.days_on_triple == 0 {
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
                validate_spread(&cost.spread, &at)?;
                non_negative(cost.slippage.points, &at("slippage.points"))?;
                non_negative(cost.slippage.bps, &at("slippage.bps"))?;
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

/// Every component: a non-empty source; not both a placeholder and excluded; a reason
/// exactly when excluded; an excluded component's amounts all zero; the carry excluded.
fn validate_provenance(
    cost: &VenueInstrumentCost,
    at: &impl Fn(&str) -> String,
) -> Result<(), ConfigError> {
    for p in cost.provenance() {
        let key = p.component.key();
        let excluded = p.status == ProvenanceStatus::Excluded;
        if p.source.trim().is_empty() {
            return Err(invalid(
                at(&format!("{key}.source")),
                "must say where the figure comes from",
            ));
        }
        let placeholder = match p.component {
            CostComponent::Spread => cost.spread.placeholder,
            CostComponent::Slippage => cost.slippage.placeholder,
            CostComponent::Commission => cost.commission.placeholder,
            CostComponent::Financing => cost.financing.placeholder,
            CostComponent::Carry => cost.carry.placeholder,
        };
        if excluded && placeholder {
            return Err(invalid(
                at(key),
                "is verified, a placeholder or excluded, never two of them",
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
            zero(&[cost.slippage.points, cost.slippage.bps]),
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
    if !cost.carry.excluded {
        return Err(invalid(
            at("carry"),
            "the benchmark or tom-next carry is not modelled: it must be excluded with a reason",
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

/// The M1 builtin figures (task #30) as a fixed arithmetic fixture for unit tests: flat
/// spreads, the archive's slippage and admin-fee-only financing, all flagged verified. The
/// ledger and metrics tests pin their hand-computed P&L to these, so a change to a venue's
/// published figures in `costs.toml` does not move them.
#[cfg(test)]
pub(crate) fn arithmetic_fixture() -> CostConfig {
    const IG: [(&str, f64, f64, f64); 5] = [
        ("EURUSD", 0.6, 0.2, 0.015),
        ("GBPJPY", 1.5, 0.2, 0.015),
        ("XAUUSD", 30.0, 5.0, 0.015),
        ("USA500IDXUSD", 0.4, 0.1, 0.034),
        ("DEUIDXEUR", 1.2, 0.2, 0.034),
    ];
    const RAZOR_SPREADS: [(&str, f64); 5] = [
        ("EURUSD", 0.1),
        ("GBPJPY", 0.5),
        ("XAUUSD", 8.0),
        ("USA500IDXUSD", 0.4),
        ("DEUIDXEUR", 1.2),
    ];
    let mut c = CostConfig::builtin();
    for venue in [
        "ig_spread_bet",
        "pepperstone_spread_bet",
        "pepperstone_razor",
    ] {
        let v = c.venues.get_mut(venue).expect("builtin venue");
        for (symbol, spread, slippage, rate) in IG {
            // Pepperstone indices: 2.5%; everything else as at IG.
            let rate = if venue != "ig_spread_bet" && rate > 0.02 {
                0.025
            } else {
                rate
            };
            let spread = RAZOR_SPREADS
                .iter()
                .find(|(s, _)| venue == "pepperstone_razor" && *s == symbol)
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
        }
    }
    c.validate().expect("the fixture is valid");
    c
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
            "ig_spread_bet",
            "pepperstone_spread_bet",
            "pepperstone_razor",
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
        "ig_spread_bet",
        "pepperstone_spread_bet",
        "pepperstone_razor",
    ];

    #[test]
    fn builtin_has_no_placeholder_for_the_five_instruments_at_any_venue() {
        // M2-R1: every component is a published figure with a dated source, or excluded
        // with a reason.
        let c = CostConfig::builtin();
        for venue in VENUES {
            assert!(c.placeholders(venue).unwrap().is_empty(), "{venue}");
            let v = c.venue(venue).unwrap();
            for symbol in REQUIRED {
                for p in v.instruments[symbol].provenance() {
                    let at = format!("{venue} {symbol} {:?}", p.component);
                    assert_ne!(p.status, ProvenanceStatus::Placeholder, "{at}");
                    assert!(p.source.contains("https://"), "{at}: {}", p.source);
                    assert!(p.source.contains("read 2026-10-07"), "{at}: {}", p.source);
                    assert!(!p.source.contains("spread_thresholds"), "{at}");
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
            let expect: &[&str] = if venue == "pepperstone_spread_bet" {
                &["GBPJPY"]
            } else {
                &[]
            };
            assert_eq!(uncosted, expect, "{venue}");
        }
    }

    #[test]
    fn builtin_ig_spreads_are_igs_published_figures() {
        // Issue #52: IG's FX averages, not the peak-hours minimums of the archive's
        // spread_thresholds.yaml; the indices by time of day; gold a minimum.
        let c = CostConfig::builtin();
        let ig = c.venue("ig_spread_bet").unwrap();
        let spread = |s: &str| &ig.instruments[s].spread;
        assert_eq!(spread("EURUSD").pips, 1.04);
        assert_eq!(spread("GBPJPY").pips, 3.86);
        for s in ["EURUSD", "GBPJPY"] {
            assert_eq!(spread(s).basis, Some(SpreadBasis::Average));
            assert!(spread(s).schedule.is_empty());
        }
        assert_eq!(spread("XAUUSD").pips, 30.0);
        assert_eq!(spread("XAUUSD").basis, Some(SpreadBasis::Minimum));
        for s in ["USA500IDXUSD", "DEUIDXEUR"] {
            assert_eq!(spread(s).tz, Some(chrono_tz::Europe::London));
            assert!(!spread(s).schedule.is_empty());
            assert_eq!(spread(s).basis, Some(SpreadBasis::Minimum));
        }
        assert_eq!(
            c.minimum_spreads("ig_spread_bet").unwrap(),
            ["DEUIDXEUR", "USA500IDXUSD", "XAUUSD"]
        );
    }

    #[test]
    fn builtin_rollovers_are_london_22_and_new_york_17() {
        let c = CostConfig::builtin();
        let ig = c.venue("ig_spread_bet").unwrap();
        assert_eq!(ig.rollover.tz, chrono_tz::Europe::London);
        assert_eq!(ig.rollover.time, NaiveTime::from_hms_opt(22, 0, 0).unwrap());
        for id in ["pepperstone_spread_bet", "pepperstone_razor"] {
            let p = c.venue(id).unwrap();
            assert_eq!(p.rollover.tz, chrono_tz::America::New_York);
            assert_eq!(p.rollover.time, NaiveTime::from_hms_opt(17, 0, 0).unwrap());
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
            // The carry is not modelled: it must be excluded.
            (
                with(
                    r#"carry = { source = "test", placeholder = false, excluded = true, reason = "not modelled" }"#,
                    r#"carry = { source = "test", placeholder = false, excluded = false }"#,
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

    #[test]
    fn from_path_reports_the_missing_file() {
        let err = CostConfig::from_path("/nonexistent/costs.toml").unwrap_err();
        assert!(matches!(err, ConfigError::Io { .. }));
    }
}
