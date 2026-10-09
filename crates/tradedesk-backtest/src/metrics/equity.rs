//! The daily equity curve, rebuilt from a finished ledger.
//!
//! A day is a UTC weekday (Monday to Friday) marked at its close, the next `00:00Z`.
//! Saturdays and Sundays are omitted, except that a window whose last day is a Saturday
//! or Sunday ends with one more point at the window's close, so the curve's last point
//! is always the window's close. A position is open at a close when
//! `entry_ts <= close < exit_ts`. It is valued at liquidation: the configured-side close
//! of the last bar closed by the instant, less the exit half-spread, slippage and exit
//! commission, exactly as [`Ledger::close`] would book it there. Financing to date is
//! the position's own [`FinancingCharge`]s before the close.

use std::collections::BTreeMap;

use chrono::{DateTime, Datelike, Days, NaiveDate, Utc, Weekday};
use serde::{Deserialize, Serialize};

use super::MetricsError;
use super::trades::CostBreakdown;
use crate::{
    ClosedTrade, Direction, Ledger, LedgerError, MarkSource, PositionId, PricePoint, TradeSide,
};

/// What an equity point closes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PointKind {
    /// A UTC weekday's close. A weekend inside the window lands in the next one.
    #[default]
    Weekday,
    /// The window's close when its last day is a Saturday or Sunday: the point after
    /// Friday's, carrying everything booked or re-marked after Friday's close.
    WeekendClose,
}

/// One close on the equity curve: a weekday's, or the window's when it ends on a
/// weekend. Money is in the account currency.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EquityPoint {
    /// The UTC day this point closes: a weekday, or the window's last day for a
    /// [`PointKind::WeekendClose`].
    pub date: NaiveDate,
    /// Which kind of close this is. Absent in records written before the weekend
    /// close existed, which read back as [`PointKind::Weekday`].
    #[serde(default)]
    pub kind: PointKind,
    /// The mark instant: `date + 1` at `00:00Z`.
    pub at: DateTime<Utc>,
    /// `starting capital + realised_net + unrealised_net`.
    pub equity: f64,
    /// Net P&L of every trade closed by `at`.
    pub realised_net: f64,
    /// Liquidation net P&L of the positions open at `at`: entry and exit costs and
    /// financing to date deducted.
    pub unrealised_net: f64,
    /// Positions open at `at`.
    pub open_positions: usize,
    /// The exit costs (half-spread, slippage, exit and round-trip commission) deducted
    /// from `unrealised_net`. A mid (`Ledger::mark_to_market`) equity is
    /// `equity + open_exit_cost`.
    pub open_exit_cost: f64,
    /// Gross P&L between marks, realised and unrealised, before any cost.
    pub gross_pnl: f64,
    /// Costs incurred by `at`: every fill booked by then and every financing charge
    /// before it. `equity = capital + gross_pnl − costs.total() − open_exit_cost`.
    pub costs: CostBreakdown,
    /// `equity / previous equity − 1`, the first against the starting capital. `None`
    /// when the previous equity is zero or below (ruin).
    pub simple_return: Option<f64>,
    /// `ln(equity / previous equity)`. `None` when either is zero or below.
    pub log_return: Option<f64>,
}

/// An open position valued at an instant. Money is in the account currency.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OpenPositionValue {
    /// Position.
    pub id: PositionId,
    /// Instrument.
    pub instrument: String,
    /// Long or short.
    pub direction: Direction,
    /// Size in instrument units.
    pub size: f64,
    /// Entry timestamp.
    pub entry_ts: DateTime<Utc>,
    /// Open of the bar whose configured-side close is the mark.
    pub mark_bar_open: DateTime<Utc>,
    /// Mark, quote units.
    pub mark_price: f64,
    /// `(mark − entry mark) × direction × size`, before any cost.
    pub unrealised_gross: f64,
    /// Liquidation value: what closing at the mark would book as net P&L.
    pub unrealised_net: f64,
    /// Exit costs deducted in `unrealised_net`.
    pub exit_cost: f64,
    /// Financing charged so far.
    pub financing: f64,
}

/// A position's life, rebuilt from the ledger's records.
struct Lot<'a> {
    id: PositionId,
    instrument: &'a str,
    direction: Direction,
    size: f64,
    entry_ts: DateTime<Utc>,
    exit_ts: Option<DateTime<Utc>>,
    entry_mark: f64,
    entry_price: f64,
    entry_commission: f64,
    /// `(rollover instant, charge in quote currency)`, oldest first.
    financing: Vec<(DateTime<Utc>, f64)>,
}

/// UTC midnight starting `date`.
pub(super) fn day_start(date: NaiveDate) -> DateTime<Utc> {
    date.and_time(chrono::NaiveTime::MIN).and_utc()
}

fn is_weekend(date: NaiveDate) -> bool {
    matches!(date.weekday(), Weekday::Sat | Weekday::Sun)
}

/// The days the curve marks from `first` to `last`, inclusive: every weekday, then
/// `last` itself as a [`PointKind::WeekendClose`] when it is a Saturday or Sunday. Empty
/// when the window holds no weekday.
pub(super) fn mark_days(first: NaiveDate, last: NaiveDate) -> Vec<(NaiveDate, PointKind)> {
    let mut days: Vec<(NaiveDate, PointKind)> = first
        .iter_days()
        .take_while(|d| *d <= last)
        .filter(|d| !is_weekend(*d))
        .map(|d| (d, PointKind::Weekday))
        .collect();
    if !days.is_empty() && is_weekend(last) {
        days.push((last, PointKind::WeekendClose));
    }
    days
}

fn rate(ledger: &Ledger, instrument: &str) -> Result<f64, LedgerError> {
    let model = ledger
        .fill_model(instrument)
        .ok_or_else(|| LedgerError::UnknownInstrument {
            venue: ledger.metadata().venue.clone(),
            instrument: instrument.to_owned(),
        })?;
    ledger
        .metadata()
        .account_fx
        .rate(&model.spec().quote_currency)
}

/// Every position the ledger knows, closed or open, with its entry commission and its
/// financing charges.
fn lots(ledger: &Ledger) -> Result<Vec<Lot<'_>>, MetricsError> {
    let mut entry_commission: BTreeMap<PositionId, f64> = BTreeMap::new();
    for f in ledger.fills() {
        if f.exit_reason.is_none() {
            entry_commission.insert(f.position, f.commission);
        }
    }
    let mut financing: BTreeMap<PositionId, Vec<(DateTime<Utc>, f64)>> = BTreeMap::new();
    for c in ledger.financing_charges() {
        financing
            .entry(c.position)
            .or_default()
            .push((c.at_utc, c.charge));
    }
    for charges in financing.values_mut() {
        charges.sort_by_key(|c| c.0);
    }
    let commission = |id| {
        entry_commission
            .get(&id)
            .copied()
            .ok_or(MetricsError::MissingEntryFill(id))
    };
    let mut out = Vec::new();
    for t in ledger.closed_trades() {
        out.push(Lot {
            id: t.id,
            instrument: &t.instrument,
            direction: t.direction,
            size: t.size,
            entry_ts: t.entry_ts,
            exit_ts: Some(t.exit_ts),
            entry_mark: t.entry_mark,
            entry_price: t.entry_price,
            entry_commission: commission(t.id)?,
            financing: financing.remove(&t.id).unwrap_or_default(),
        });
    }
    for p in ledger.open_positions() {
        out.push(Lot {
            id: p.id,
            instrument: &p.instrument,
            direction: p.direction,
            size: p.size,
            entry_ts: p.entry_ts,
            exit_ts: None,
            entry_mark: p.entry_mark,
            entry_price: p.entry_price,
            entry_commission: p.entry_commission,
            financing: financing.remove(&p.id).unwrap_or_default(),
        });
    }
    Ok(out)
}

/// Value `lot` at liquidation at `at`.
fn liquidation_value(
    ledger: &Ledger,
    lot: &Lot<'_>,
    at: DateTime<Utc>,
    marks: &impl MarkSource,
) -> Result<OpenPositionValue, LedgerError> {
    let model =
        ledger
            .fill_model(lot.instrument)
            .ok_or_else(|| LedgerError::UnknownInstrument {
                venue: ledger.metadata().venue.clone(),
                instrument: lot.instrument.to_owned(),
            })?;
    let rate = rate(ledger, lot.instrument)?;
    let bar = marks
        .bar_closed_by(lot.instrument, at)
        .ok_or_else(|| LedgerError::NoMark {
            instrument: lot.instrument.to_owned(),
            at,
        })?;
    let side = match lot.direction {
        Direction::Long => TradeSide::Sell,
        Direction::Short => TradeSide::Buy,
    };
    crate::ledger::checked_mark(model, bar)?;
    let q = model.quote(bar, PricePoint::Close, at, side, lot.size)?;
    let exit_commission = q.commission + model.cost().commission.per_round_trip;
    let financing: f64 = lot
        .financing
        .iter()
        .take_while(|(ts, _)| *ts < at)
        .map(|(_, c)| c)
        .sum();
    let s = lot.direction.sign();
    let gross = (q.mark_price - lot.entry_mark) * s * lot.size;
    // The same arithmetic as `Ledger::close`'s `net_pnl_quote`.
    let net = (q.fill_price - lot.entry_price) * s * lot.size
        - financing
        - (lot.entry_commission + exit_commission) / rate;
    Ok(OpenPositionValue {
        id: lot.id,
        instrument: lot.instrument.to_owned(),
        direction: lot.direction,
        size: lot.size,
        entry_ts: lot.entry_ts,
        mark_bar_open: bar.ts_open_utc,
        mark_price: q.mark_price,
        unrealised_gross: gross * rate,
        unrealised_net: net * rate,
        exit_cost: (q.spread_cost + q.slippage_cost) * rate + exit_commission,
        financing: financing * rate,
    })
}

/// The positions open at `at`, each valued at liquidation.
pub(super) fn open_at(
    ledger: &Ledger,
    at: DateTime<Utc>,
    marks: &impl MarkSource,
) -> Result<Vec<OpenPositionValue>, MetricsError> {
    let lots = lots(ledger)?;
    let mut out = Vec::new();
    for lot in lots.iter().filter(|l| is_open(l, at)) {
        out.push(liquidation_value(ledger, lot, at, marks)?);
    }
    out.sort_by_key(|v| v.id);
    Ok(out)
}

fn is_open(lot: &Lot<'_>, at: DateTime<Utc>) -> bool {
    lot.entry_ts <= at && lot.exit_ts.is_none_or(|exit| exit > at)
}

/// The equity curve over `days` (from [`mark_days`], ascending) from
/// `starting_capital`. Each day is marked at the next `00:00Z`. `ledger` must already
/// carry financing accrued to the last close.
pub(super) fn build_curve(
    ledger: &Ledger,
    marks: &impl MarkSource,
    days: &[(NaiveDate, PointKind)],
    starting_capital: f64,
) -> Result<Vec<EquityPoint>, MetricsError> {
    let lots = lots(ledger)?;

    // Cost events in time order, each already in the account currency.
    let mut fill_costs = Vec::with_capacity(ledger.fills().len());
    for f in ledger.fills() {
        let r = rate(ledger, &f.instrument)?;
        fill_costs.push((f.ts, f.spread_cost * r, f.slippage_cost * r, f.commission));
    }
    fill_costs.sort_by_key(|c| c.0);
    let mut financing = Vec::with_capacity(ledger.financing_charges().len());
    for c in ledger.financing_charges() {
        financing.push((c.at_utc, c.charge * rate(ledger, &c.instrument)?));
    }
    financing.sort_by_key(|c| c.0);
    let mut closed: Vec<&ClosedTrade> = ledger.closed_trades().iter().collect();
    closed.sort_by_key(|t| t.exit_ts);

    let (mut next_fill, mut next_charge, mut next_close) = (0, 0, 0);
    let mut costs = CostBreakdown::default();
    let (mut realised_net, mut realised_gross) = (0.0, 0.0);
    let mut previous = starting_capital;
    let mut points = Vec::with_capacity(days.len());
    for &(date, kind) in days {
        let at = day_start(date + Days::new(1));
        while let Some(&(ts, spread, slippage, commission)) = fill_costs.get(next_fill) {
            if ts > at {
                break;
            }
            costs.spread += spread;
            costs.slippage += slippage;
            costs.commission += commission;
            next_fill += 1;
        }
        while let Some(&(ts, charge)) = financing.get(next_charge) {
            if ts >= at {
                break;
            }
            costs.financing += charge;
            next_charge += 1;
        }
        while let Some(t) = closed.get(next_close) {
            if t.exit_ts > at {
                break;
            }
            realised_net += t.net_pnl_account;
            realised_gross += t.gross_pnl_account;
            next_close += 1;
        }
        let (mut unrealised_net, mut unrealised_gross, mut exit_cost, mut open) =
            (0.0, 0.0, 0.0, 0);
        for lot in lots.iter().filter(|l| is_open(l, at)) {
            let v = liquidation_value(ledger, lot, at, marks)?;
            unrealised_net += v.unrealised_net;
            unrealised_gross += v.unrealised_gross;
            exit_cost += v.exit_cost;
            open += 1;
        }
        let equity = starting_capital + realised_net + unrealised_net;
        points.push(EquityPoint {
            date,
            kind,
            at,
            equity,
            realised_net,
            unrealised_net,
            open_positions: open,
            open_exit_cost: exit_cost,
            gross_pnl: realised_gross + unrealised_gross,
            costs,
            simple_return: (previous > 0.0).then(|| equity / previous - 1.0),
            log_return: (previous > 0.0 && equity > 0.0).then(|| (equity / previous).ln()),
        });
        previous = equity;
    }
    Ok(points)
}
