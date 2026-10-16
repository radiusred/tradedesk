//! The volatility slippage and the spread excess (task #195, specification §4 items 1 to 5
//! and 8) on labelled synthetic one-minute data. The coefficients 0.25 / 0.5 / 0.0 and
//! the cap 4.0 are test values, not venue figures.
//!
//! The instrument is the example EURUSD (raw scale and pip 0.0001) at the example spread
//! bet, flattened to a 0.6-pip spread. A synthetic minute is given by its mid, the range
//! of its mid and its ask − bid, all in raw units.
#![allow(clippy::cast_precision_loss)]

use std::sync::Arc;

use chrono::{DateTime, Duration, TimeZone, Utc};
use proptest::prelude::*;

use super::config::{CostConfig, SlippageModel, SpreadExcess, VenueInstrumentCost};
use super::fill::{
    FillError, FillKind, FillModel, FillQuote, PricePoint, SpreadRatioStatus, TradeSide,
};
use super::minutes::{MinuteCosts, MinutePrices};
use crate::{BarTimeframe, JoinedBar, Ohlcv};

/// Half the flattened 0.6-pip spread, quote price.
const HALF_SPREAD: f64 = 0.000_03;

fn close(a: f64, b: f64) -> bool {
    (a - b).abs() < 1e-12 * a.abs().max(b.abs()).max(1.0)
}

/// A Wednesday, so that 20 weekdays of baseline lie before it.
fn utc(d: u32, h: u32, mi: u32) -> DateTime<Utc> {
    Utc.with_ymd_and_hms(2024, 3, d, h, mi, 0).unwrap()
}

fn side(open: f64, high: f64, low: f64, close: f64) -> Ohlcv {
    Ohlcv {
        open,
        high,
        low,
        close,
        tick_volume: 1.0,
    }
}

/// A labelled synthetic minute: mid `mid`, mid range `range`, ask − bid `spread`.
fn synthetic_minute(ts: DateTime<Utc>, mid: f64, range: f64, spread: f64) -> MinutePrices {
    let b = mid - spread / 2.0;
    let a = mid + spread / 2.0;
    MinutePrices {
        ts_open_utc: ts,
        bid: Some(side(b, b + range / 2.0, b - range / 2.0, b)),
        ask: Some(side(a, a + range / 2.0, a - range / 2.0, a)),
    }
}

/// A synthetic one-hour engine bar opening at `ts`, flat at mid `mid` except a low of
/// `low`, with a 0.2 raw ask − bid.
fn synthetic_hour(ts: DateTime<Utc>, mid: f64, low: f64) -> JoinedBar {
    JoinedBar {
        ts_open_utc: ts,
        bid: side(mid - 0.1, mid - 0.1, low - 0.1, mid - 0.1),
        ask: side(mid + 0.1, mid + 0.1, low + 0.1, mid + 0.1),
    }
}

/// The synthetic test coefficients: market 0.25, stop 0.5, limit 0.0.
fn synthetic_volatility(cost: &mut VenueInstrumentCost) {
    let s = &mut cost.slippage;
    s.model = SlippageModel::Volatility;
    s.points = 0.0;
    s.bps = 0.0;
    s.market = Some(0.25);
    s.stop = Some(0.5);
    s.limit = Some(0.0);
    s.excluded = false;
    s.reason = None;
}

/// The synthetic test spread excess: 20 baseline weekdays, cap 4.0.
fn synthetic_excess() -> SpreadExcess {
    SpreadExcess {
        baseline_weekdays: None,
        cap: 4.0,
        source: "synthetic test value".to_owned(),
        placeholder: false,
        excluded: false,
        reason: None,
        estimated: true,
    }
}

fn model(volatility: bool, excess: Option<SpreadExcess>) -> FillModel {
    let c = CostConfig::builtin();
    let mut cost = c.venue("example_spread_bet").unwrap().instruments["EURUSD"].clone();
    cost.spread.pips = 0.6;
    if volatility {
        synthetic_volatility(&mut cost);
    }
    cost.spread_excess = excess;
    FillModel::new("EURUSD", c.instrument("EURUSD").unwrap().clone(), cost)
}

fn attached(mut m: FillModel, minutes: Vec<MinutePrices>) -> FillModel {
    m.attach_minutes(
        Arc::new(MinuteCosts::new(minutes).unwrap()),
        BarTimeframe::H1,
    );
    m
}

/// Thirty minutes before `at` with ranges 1, 2, …, 30 (median 15.5), mid 11000, ask − bid
/// 0.2, then `exec` from `at` on.
fn floor_then(at: DateTime<Utc>, exec: &[(i64, f64)]) -> Vec<MinutePrices> {
    let mut out: Vec<MinutePrices> = (1..=30)
        .map(|k| synthetic_minute(at - Duration::minutes(31 - k), 11_000.0, k as f64, 0.2))
        .collect();
    out.extend(
        exec.iter()
            .map(|&(m, r)| synthetic_minute(at + Duration::minutes(m), 11_000.0, r, 0.2)),
    );
    out
}

fn quote(
    m: &FillModel,
    bar: &JoinedBar,
    point: PricePoint,
    at: DateTime<Utc>,
    kind: FillKind,
    trade: TradeSide,
) -> FillQuote {
    m.quote_as(bar, point, at, trade, 10_000.0, kind).unwrap()
}

#[test]
fn golden_market_fill_buys_and_sells_at_the_mark_plus_half_spread_and_slippage() {
    // §4.1: τ = the close of the 13:00 bar. Its execution minute (14:00) has a range of
    // 40 raw, above the floor of 15.5: R_used = 40 raw = 0.004, slippage 0.25 × 0.004.
    let at = utc(13, 14, 0);
    let m = attached(model(true, None), floor_then(at, &[(0, 40.0), (1, 2.0)]));
    let bar = synthetic_hour(utc(13, 13, 0), 11_000.0, 11_000.0);
    let buy = quote(
        &m,
        &bar,
        PricePoint::Close,
        at,
        FillKind::Market,
        TradeSide::Buy,
    );
    let d = buy.detail.unwrap();
    assert_eq!(d.fill_kind, FillKind::Market);
    assert_eq!(d.slippage_model, SlippageModel::Volatility);
    assert_eq!(d.exec_minute, Some(at));
    assert!(close(d.range_exec.unwrap(), 0.004));
    assert!(close(d.range_floor.unwrap(), 0.001_55));
    assert!(close(d.range_used.unwrap(), 0.004));
    assert_eq!(d.kappa, Some(0.25));
    assert!(close(buy.slippage, 0.001));
    assert!(close(buy.mark_price, 1.1));
    assert!(close(buy.fill_price, 1.1 + HALF_SPREAD + 0.001));
    assert!(close(buy.slippage_cost, 10.0));
    assert_eq!(d.spread_ratio_status, SpreadRatioStatus::Absent);
    assert_eq!(buy.spread_excess_cost, 0.0);
    // Spec revision 2: the venue half-spread is recorded; no spread excess, no baseline.
    assert!(close(d.half_spread, HALF_SPREAD));
    assert_eq!(d.spread_baseline, None);
    let sell = quote(
        &m,
        &bar,
        PricePoint::Close,
        at,
        FillKind::Market,
        TradeSide::Sell,
    );
    assert!(close(sell.fill_price, 1.1 - HALF_SPREAD - 0.001));
}

#[test]
fn a_quiet_execution_minute_pays_the_floor_and_the_floor_counts_bars_across_a_weekend() {
    // §4.2: a zero-range execution minute pays the median of the 30 before it.
    let at = utc(13, 14, 0);
    let m = attached(model(true, None), floor_then(at, &[(0, 0.0)]));
    let bar = synthetic_hour(utc(13, 13, 0), 11_000.0, 11_000.0);
    let q = quote(
        &m,
        &bar,
        PricePoint::Close,
        at,
        FillKind::Market,
        TradeSide::Buy,
    );
    let d = q.detail.unwrap();
    assert_eq!(d.range_exec, Some(0.0));
    assert!(close(d.range_used.unwrap(), 0.001_55));
    assert!(close(q.slippage, 0.25 * 0.001_55));

    // A fill at Monday 00:00 after a long weekend: the 30 bars are Friday's last 30
    // minutes (ranges 1 to 30), more than two days back.
    let monday = utc(18, 0, 0);
    let friday = utc(15, 22, 0);
    let mut minutes = floor_then(friday, &[]);
    minutes.push(synthetic_minute(monday, 11_000.0, 0.0, 0.2));
    let m = attached(model(true, None), minutes);
    let bar = synthetic_hour(utc(17, 23, 0), 11_000.0, 11_000.0);
    let d = quote(
        &m,
        &bar,
        PricePoint::Close,
        monday,
        FillKind::Market,
        TradeSide::Buy,
    )
    .detail
    .unwrap();
    assert!(close(d.range_floor.unwrap(), 0.001_55));
}

#[test]
fn a_gap_in_the_minutes_charges_the_floor_alone_and_no_minutes_is_an_error() {
    // §4.3: the next minute opens 5 minutes after τ, outside [τ, τ + 5).
    let at = utc(13, 14, 0);
    let m = attached(model(true, None), floor_then(at, &[(5, 80.0)]));
    let bar = synthetic_hour(utc(13, 13, 0), 11_000.0, 11_000.0);
    let d = quote(
        &m,
        &bar,
        PricePoint::Close,
        at,
        FillKind::Market,
        TradeSide::Buy,
    )
    .detail
    .unwrap();
    assert_eq!(d.exec_minute, None);
    assert_eq!(d.range_exec, None);
    assert!(close(d.range_used.unwrap(), 0.001_55));
    // With one minute inside the gap it is the execution minute.
    let m = attached(model(true, None), floor_then(at, &[(4, 80.0)]));
    let d = quote(
        &m,
        &bar,
        PricePoint::Close,
        at,
        FillKind::Market,
        TradeSide::Buy,
    )
    .detail
    .unwrap();
    assert_eq!(d.exec_minute, Some(at + Duration::minutes(4)));
    // Nothing before τ and nothing within the gap.
    let m = attached(
        model(true, None),
        vec![synthetic_minute(
            at + Duration::minutes(9),
            11_000.0,
            1.0,
            0.2,
        )],
    );
    assert!(matches!(
        m.quote_as(
            &bar,
            PricePoint::Close,
            at,
            TradeSide::Buy,
            1.0,
            FillKind::Market
        ),
        Err(FillError::NoVolatility { .. })
    ));
    // No minute data attached at all.
    assert!(matches!(
        model(true, None).quote(&bar, PricePoint::Close, at, TradeSide::Buy, 1.0),
        Err(FillError::NoMinuteData { .. })
    ));
}

/// A synthetic hour from 10:00 built from 60 minutes at mid 11000 with ranges of 2 raw,
/// except a dip to a low of 10990 at 10:20 and to 10980 at 10:40, and the hour's bar.
fn stop_hour() -> (Vec<MinutePrices>, JoinedBar) {
    let open = utc(13, 10, 0);
    let mut minutes = Vec::new();
    for k in 0..60 {
        let ts = open + Duration::minutes(k);
        minutes.push(match k {
            20 => synthetic_minute(ts, 10_995.0, 10.0, 0.2), // low 10990, range 10
            40 => synthetic_minute(ts, 10_990.0, 20.0, 0.2), // low 10980, range 20
            _ => synthetic_minute(ts, 11_000.0, 2.0, 0.2),
        });
    }
    (minutes, synthetic_hour(open, 11_000.0, 10_980.0))
}

#[test]
fn a_stop_executes_in_the_first_minute_that_reaches_it_and_a_gap_in_the_first() {
    // §4.4. A long's stop at 10985 inside the bar: 10:20 does not reach it, 10:40 does.
    let (minutes, bar) = stop_hour();
    let m = attached(model(true, None), minutes);
    let at = utc(13, 11, 0); // the engine stamps a level fill with the bar close
    let q = quote(
        &m,
        &bar,
        PricePoint::Level(10_985.0),
        at,
        FillKind::Stop,
        TradeSide::Sell,
    );
    let d = q.detail.unwrap();
    assert_eq!(d.exec_minute, Some(utc(13, 10, 40)));
    assert!(close(d.range_exec.unwrap(), 0.002));
    assert_eq!(d.kappa, Some(0.5));
    // The floor is the 30 minutes before 11:00: 29 of range 2 and the 10:40 dip.
    assert!(close(d.range_floor.unwrap(), 0.0002));
    assert!(close(q.slippage, 0.5 * 0.002));
    assert!(close(q.fill_price, 1.0985 - HALF_SPREAD - 0.001));

    // A gap open through the stop fills at the open, in the bar's first minute.
    let q = quote(
        &m,
        &bar,
        PricePoint::Open,
        bar.ts_open_utc,
        FillKind::Stop,
        TradeSide::Sell,
    );
    let d = q.detail.unwrap();
    assert_eq!(d.exec_minute, Some(bar.ts_open_utc));
    assert_eq!(d.kappa, Some(0.5));
    assert!(close(d.range_exec.unwrap(), 0.0002));

    // A target (limit 0.0) charges no slippage.
    let q = quote(
        &m,
        &bar,
        PricePoint::Level(10_985.0),
        at,
        FillKind::Limit,
        TradeSide::Sell,
    );
    assert_eq!(q.detail.unwrap().kappa, Some(0.0));
    assert_eq!(q.slippage, 0.0);
    assert!(close(q.fill_price, 1.0985 - HALF_SPREAD));
}

#[test]
fn a_level_no_minute_reaches_executes_in_the_minute_of_nearest_approach() {
    let (minutes, bar) = stop_hour();
    let m = attached(model(true, None), minutes);
    let q = quote(
        &m,
        &bar,
        PricePoint::Level(10_979.0),
        utc(13, 11, 0),
        FillKind::Stop,
        TradeSide::Sell,
    );
    assert_eq!(q.detail.unwrap().exec_minute, Some(utc(13, 10, 40)));
    // A level above the open scans the highs: the first minute reaching 11001 is the
    // first minute (high 11001).
    let q = quote(
        &m,
        &bar,
        PricePoint::Level(11_001.0),
        utc(13, 11, 0),
        FillKind::Limit,
        TradeSide::Sell,
    );
    assert_eq!(q.detail.unwrap().exec_minute, Some(utc(13, 10, 0)));
}

/// Twenty synthetic weekdays before 2024-03-13 with an ask − bid of 2 raw at 14:00, the
/// floor's 30 minutes before 14:00 on the 13th, and the 14:00 minute at `spread`.
fn baseline_then(spread: f64, weekdays: usize) -> Vec<MinutePrices> {
    let at = utc(13, 14, 0);
    let mut days = Vec::new();
    let mut day = at.date_naive();
    while days.len() < weekdays {
        day = day.pred_opt().unwrap();
        if !matches!(day.format("%a").to_string().as_str(), "Sat" | "Sun") {
            days.push(day);
        }
    }
    days.reverse();
    let mut out: Vec<MinutePrices> = days
        .iter()
        .map(|d| {
            synthetic_minute(
                d.and_hms_opt(14, 0, 0).unwrap().and_utc(),
                11_000.0,
                1.0,
                2.0,
            )
        })
        .collect();
    out.extend(floor_then(at, &[]));
    out.push(synthetic_minute(at, 11_000.0, 1.0, spread));
    out
}

fn excess_at(minutes: Vec<MinutePrices>) -> (FillQuote, super::fill::FillCostDetail) {
    let m = attached(model(false, Some(synthetic_excess())), minutes);
    let bar = synthetic_hour(utc(13, 13, 0), 11_000.0, 11_000.0);
    let q = quote(
        &m,
        &bar,
        PricePoint::Close,
        utc(13, 14, 0),
        FillKind::Market,
        TradeSide::Buy,
    );
    (q, q.detail.unwrap())
}

#[test]
fn the_spread_excess_charges_the_venue_half_spread_times_the_excess_ratio() {
    // §4.5: three times the baseline: w = 3, extra = 2 × half spread.
    let (q, d) = excess_at(baseline_then(6.0, 20));
    assert_eq!(d.slippage_model, SlippageModel::Fixed);
    assert_eq!(d.spread_ratio, Some(3.0));
    assert_eq!(d.spread_ratio_status, SpreadRatioStatus::Ok);
    assert!(close(q.extra_half_spread, 2.0 * HALF_SPREAD));
    // Spec revision 2: the baseline (2.0 raw = 0.0002) and the venue half-spread are
    // recorded, and they recompute the charge.
    assert!(close(d.spread_baseline.unwrap(), 0.000_2));
    assert!(close(d.half_spread, HALF_SPREAD));
    assert!(close(
        d.extra_half_spread,
        d.half_spread * (d.spread_ratio.unwrap().min(4.0) - 1.0)
    ));
    assert!(close(q.spread_excess_cost, 2.0 * HALF_SPREAD * 10_000.0));
    assert!(close(q.fill_price, 1.1 + 3.0 * HALF_SPREAD));
    // The venue spread itself is unchanged; the extra is booked on its own.
    assert!(close(q.spread_cost, HALF_SPREAD * 10_000.0));
    // Ten times: capped at 4, extra = 3 × half spread.
    let (q, d) = excess_at(baseline_then(20.0, 20));
    assert_eq!(d.spread_ratio, Some(10.0));
    assert_eq!(d.spread_ratio_status, SpreadRatioStatus::Capped);
    assert!(close(q.extra_half_spread, 3.0 * HALF_SPREAD));
    assert!(close(d.spread_baseline.unwrap(), 0.000_2));
    assert!(close(d.half_spread, HALF_SPREAD));
    // Narrower than usual: no credit.
    let (q, d) = excess_at(baseline_then(1.0, 20));
    assert_eq!(d.spread_ratio, Some(0.5));
    assert_eq!(q.extra_half_spread, 0.0);
}

#[test]
fn every_spread_ratio_status_has_its_case() {
    // Ten baseline values are enough (20 / 2), nine are not.
    let (_, d) = excess_at(baseline_then(6.0, 10));
    assert_eq!(d.spread_ratio_status, SpreadRatioStatus::Ok);
    let (q, d) = excess_at(baseline_then(6.0, 9));
    assert_eq!(d.spread_ratio_status, SpreadRatioStatus::BaselineMissing);
    assert_eq!((d.spread_ratio, q.extra_half_spread), (None, 0.0));
    // Spec revision 2: no baseline exists, so `spread_baseline` is null in the JSON.
    assert_eq!(d.spread_baseline, None);
    assert!(close(d.half_spread, HALF_SPREAD));
    let json = serde_json::to_value(d).unwrap();
    assert_eq!(json.get("spread_baseline"), Some(&serde_json::Value::Null));
    // A baseline of zero.
    let zero: Vec<MinutePrices> = baseline_then(6.0, 20)
        .into_iter()
        .map(|mut m| {
            if m.ts_open_utc < utc(13, 0, 0) {
                m.ask = m.bid;
            }
            m
        })
        .collect();
    let (_, d) = excess_at(zero);
    assert_eq!(
        d.spread_ratio_status,
        SpreadRatioStatus::BaselineNonpositive
    );
    assert_eq!(d.spread_baseline, Some(0.0));
    // A one-sided execution minute.
    let mut one_sided = baseline_then(6.0, 20);
    one_sided.last_mut().unwrap().ask = None;
    let (_, d) = excess_at(one_sided);
    assert_eq!(d.spread_ratio_status, SpreadRatioStatus::OneSided);
    // No execution minute.
    let mut none = baseline_then(6.0, 20);
    none.pop();
    let (q, d) = excess_at(none);
    assert_eq!(d.spread_ratio_status, SpreadRatioStatus::NoExecMinute);
    assert_eq!(q.extra_half_spread, 0.0);
    // No spread excess configured: absent, and the fill is exactly the fixed one.
    let at = utc(13, 14, 0);
    let bar = synthetic_hour(utc(13, 13, 0), 11_000.0, 11_000.0);
    let m = attached(model(true, None), baseline_then(6.0, 20));
    let d = quote(
        &m,
        &bar,
        PricePoint::Close,
        at,
        FillKind::Market,
        TradeSide::Buy,
    )
    .detail
    .unwrap();
    assert_eq!(d.spread_ratio_status, SpreadRatioStatus::Absent);
    // An excluded spread excess charges nothing either.
    let mut excluded = synthetic_excess();
    excluded.excluded = true;
    excluded.estimated = false;
    excluded.reason = Some("synthetic".to_owned());
    let m = attached(model(false, Some(excluded)), baseline_then(6.0, 20));
    let q = quote(
        &m,
        &bar,
        PricePoint::Close,
        at,
        FillKind::Market,
        TradeSide::Buy,
    );
    assert_eq!(
        q.detail.unwrap().spread_ratio_status,
        SpreadRatioStatus::Absent
    );
    assert_eq!(q.extra_half_spread, 0.0);
}

#[test]
fn the_baseline_looks_back_at_most_twice_its_weekday_count() {
    // Twenty values, but the first nine of them more than 40 weekdays back: eleven count,
    // which is enough; with eleven too far back, nine count, which is not.
    let at = utc(13, 14, 0);
    let bar = synthetic_hour(utc(13, 13, 0), 11_000.0, 11_000.0);
    let spaced = |too_far: usize| {
        let mut days = Vec::new();
        let mut day = at.date_naive();
        let mut weekdays = 0;
        while days.len() < 20 {
            day = day.pred_opt().unwrap();
            if matches!(day.format("%a").to_string().as_str(), "Sat" | "Sun") {
                continue;
            }
            weekdays += 1;
            // The recent ones on every other weekday; the rest beyond weekday 40.
            if (days.len() < 20 - too_far && weekdays % 2 == 0) || weekdays > 40 {
                days.push(day);
            }
        }
        days.reverse();
        let mut out: Vec<MinutePrices> = days
            .iter()
            .map(|d| {
                synthetic_minute(
                    d.and_hms_opt(14, 0, 0).unwrap().and_utc(),
                    11_000.0,
                    1.0,
                    2.0,
                )
            })
            .collect();
        out.extend(floor_then(at, &[]));
        out.push(synthetic_minute(at, 11_000.0, 1.0, 6.0));
        out
    };
    for (too_far, status) in [
        (9, SpreadRatioStatus::Ok),
        (11, SpreadRatioStatus::BaselineMissing),
    ] {
        let m = attached(model(false, Some(synthetic_excess())), spaced(too_far));
        let d = quote(
            &m,
            &bar,
            PricePoint::Close,
            at,
            FillKind::Market,
            TradeSide::Buy,
        )
        .detail
        .unwrap();
        assert_eq!(d.spread_ratio_status, status, "{too_far}");
    }
}

proptest! {
    #[test]
    fn slippage_and_extra_are_adverse_and_non_decreasing_in_coefficients_and_cap(
        exec_range in 0.0f64..100.0,
        exec_spread in 0.0f64..40.0,
        market in 0.0f64..2.0,
        bump in 0.0f64..2.0,
        cap in 1.0f64..10.0,
        cap_bump in 0.0f64..10.0,
    ) {
        // §4.8 on the golden market fill's synthetic minutes.
        let at = utc(13, 14, 0);
        let bar = synthetic_hour(utc(13, 13, 0), 11_000.0, 11_000.0);
        let mut minutes = baseline_then(exec_spread, 20);
        let last = minutes.last_mut().unwrap();
        *last = synthetic_minute(at, 11_000.0, exec_range, exec_spread);
        let with = |market: f64, cap: f64| {
            let mut x = synthetic_excess();
            x.cap = cap;
            let mut m = model(true, Some(x));
            let mut cost = m.cost().clone();
            cost.slippage.market = Some(market);
            m = FillModel::new("EURUSD", m.spec().clone(), cost);
            attached(m, minutes.clone())
        };
        let base = with(market, cap);
        let more = with(market + bump, cap + cap_bump);
        for trade in [TradeSide::Buy, TradeSide::Sell] {
            let q = quote(&base, &bar, PricePoint::Close, at, FillKind::Market, trade);
            let r = quote(&more, &bar, PricePoint::Close, at, FillKind::Market, trade);
            prop_assert!(q.slippage >= 0.0 && q.extra_half_spread >= 0.0);
            prop_assert!(trade.sign() * (q.fill_price - q.mark_price) >= 0.0);
            prop_assert!(r.slippage >= q.slippage);
            prop_assert!(r.extra_half_spread >= q.extra_half_spread);
            prop_assert!(trade.sign() * (r.fill_price - q.fill_price) >= -1e-15);
        }
    }
}

/// The example spread bet with EURUSD on the synthetic volatility slippage and spread
/// excess (1.0-pip venue spread, half 0.00005).
fn synthetic_config() -> CostConfig {
    let text = super::config::BUILTIN_COSTS_TOML.replacen(
        r#"slippage = { placeholder = false, excluded = true, reason = "example: no slippage figure, so costed results omit it", source = "example, not a venue's" }"#,
        r#"slippage = { model = "volatility", market = 0.25, stop = 0.5, limit = 0.0, placeholder = false, estimated = true, source = "synthetic test values" }
spread_excess = { cap = 4.0, placeholder = false, estimated = true, source = "synthetic test value" }"#,
        1,
    );
    let c = CostConfig::from_toml_str(&text).unwrap();
    assert!(c.venue("example_spread_bet").unwrap().instruments["EURUSD"].needs_minute_costs());
    c
}

/// Synthetic minutes for the ledger tests on 2024-03-13: a 2-raw ask − bid baseline at
/// 14:00 and 15:00 on the 20 weekdays before; ranges 1 to 30 before 14:00 and a 14:00
/// minute of range 40 at three times the baseline spread; range 4 before 15:00 and a
/// 15:00 minute of range 8; range 6 before midnight and a midnight minute of range 10.
fn synthetic_day() -> Vec<MinutePrices> {
    let mut out: Vec<MinutePrices> = Vec::new();
    let mut day = utc(13, 0, 0).date_naive();
    let mut days = Vec::new();
    while days.len() < 20 {
        day = day.pred_opt().unwrap();
        if !matches!(day.format("%a").to_string().as_str(), "Sat" | "Sun") {
            days.push(day);
        }
    }
    days.reverse();
    for d in days {
        for h in [14, 15] {
            let ts = d.and_hms_opt(h, 0, 0).unwrap().and_utc();
            out.push(synthetic_minute(ts, 11_000.0, 1.0, 2.0));
        }
    }
    out.extend(floor_then(utc(13, 14, 0), &[]));
    out.push(synthetic_minute(utc(13, 14, 0), 11_000.0, 40.0, 6.0));
    let flat = |from: DateTime<Utc>, range: f64| {
        (0..30).map(move |k| synthetic_minute(from + Duration::minutes(k), 11_000.0, range, 2.0))
    };
    out.extend(flat(utc(13, 14, 30), 4.0));
    out.push(synthetic_minute(utc(13, 15, 0), 11_000.0, 8.0, 2.0));
    out.extend(flat(utc(13, 23, 30), 6.0));
    out.push(synthetic_minute(utc(14, 0, 0), 11_000.0, 10.0, 2.0));
    out
}

fn synthetic_hours() -> crate::JoinedSeries {
    let bars = (0..24)
        .map(|h| synthetic_hour(utc(13, h, 0), 11_000.0, 11_000.0))
        .collect();
    crate::JoinedSeries::new("EURUSD", BarTimeframe::H1, bars).unwrap()
}

fn synthetic_ledger() -> crate::Ledger {
    use crate::{AccountFx, BothHit, ExitEvaluation, Ledger};
    let mut l = Ledger::new(
        &synthetic_config(),
        "example_spread_bet",
        ExitEvaluation::Intrabar {
            both_hit: BothHit::StopFirst,
        },
        AccountFx::new("GBP", [("USD".to_owned(), 0.8)]).unwrap(),
    )
    .unwrap();
    l.attach_minutes(
        "EURUSD",
        Arc::new(MinuteCosts::new(synthetic_day()).unwrap()),
        BarTimeframe::H1,
    )
    .unwrap();
    l
}

fn open_long(l: &mut crate::Ledger, hours: &crate::JoinedSeries) -> crate::PositionId {
    use crate::{Direction, FillAt, OpenOrder};
    l.open(
        &OpenOrder {
            instrument: "EURUSD".to_owned(),
            direction: Direction::Long,
            size: 10_000.0,
            stop: None,
            target: None,
        },
        FillAt {
            bar: &hours.bars()[13],
            point: PricePoint::Close,
            ts: utc(13, 14, 0),
            bar_index: 13,
        },
    )
    .unwrap()
}

#[test]
fn a_round_trip_books_the_detail_and_nets_the_spread_excess() {
    use crate::{ExitReason, FillAt};
    let hours = synthetic_hours();
    let mut l = synthetic_ledger();
    let id = open_long(&mut l, &hours);
    // A close-only stop at the 14:00 bar's close: the market rule finds 15:00, the stop
    // coefficient applies. R_used = max(8, 4) raw; w = 2 / 2 = 1, no extra.
    let t = l
        .close(
            id,
            FillAt {
                bar: &hours.bars()[14],
                point: PricePoint::Close,
                ts: utc(13, 15, 0),
                bar_index: 14,
            },
            ExitReason::Stop,
            &hours,
        )
        .unwrap()
        .clone();
    // Entry 1.1 + 0.00005 + 0.0001 (w = 3) + 0.25 × 0.004; exit 1.1 − 0.00005 − 0.5 × 0.0008.
    assert!(close(t.entry_price, 1.101_15));
    assert!(close(t.exit_price, 1.099_55));
    assert!(close(t.spread_cost, 1.0));
    assert!(close(t.slippage_cost, 14.0));
    assert!(close(t.spread_excess_cost.unwrap(), 1.0));
    assert_eq!(t.gross_pnl_quote, 0.0);
    assert!(close(t.net_pnl_quote, -16.0));
    assert!(close(
        t.net_pnl_quote,
        t.gross_pnl_quote
            - t.spread_cost
            - t.spread_excess_cost.unwrap()
            - t.slippage_cost
            - t.financing
            - t.commission / t.fx_rate
    ));
    let fills = l.fills();
    let entry = fills[0].cost_detail.unwrap();
    let exit = fills[1].cost_detail.unwrap();
    assert_eq!(
        (entry.fill_kind, exit.fill_kind),
        (FillKind::Market, FillKind::Stop)
    );
    assert_eq!(entry.spread_ratio, Some(3.0));
    assert_eq!(exit.exec_minute, Some(utc(13, 15, 0)));
    assert_eq!(exit.kappa, Some(0.5));
    // The detail is flattened into the fill's JSON and reads back.
    let json = serde_json::to_value(&fills[1]).unwrap();
    for key in [
        "fill_kind",
        "slippage_model",
        "exec_minute",
        "range_exec",
        "range_floor",
        "range_used",
        "kappa",
        "slippage",
        "spread_ratio",
        "spread_baseline",
        "spread_ratio_status",
        "half_spread",
        "extra_half_spread",
        "spread_excess_cost",
    ] {
        assert!(json.get(key).is_some(), "{key}");
    }
    assert_eq!(json["fill_kind"], "stop");
    assert_eq!(json["spread_ratio_status"], "ok");
    assert!(close(
        json["half_spread"].as_f64().unwrap(),
        exit.half_spread
    ));
    let back: crate::Fill = serde_json::from_value(json).unwrap();
    assert_eq!(back, fills[1]);
    assert_eq!(l.metadata().estimates.len(), 2);
}

#[test]
fn the_liquidation_valuation_charges_the_market_coefficient_at_the_mark_instant() {
    // §4.6: the position is still open at the window's close, 2024-03-14 00:00Z. The
    // valuation is a market fill there: the 00:00 minute (range 10 raw) over the floor of
    // 6, so 0.25 × 0.001; no baseline at 00:00, so no extra.
    use crate::{Metrics, MetricsConfig};
    let hours = synthetic_hours();
    let mut l = synthetic_ledger();
    open_long(&mut l, &hours);
    let day = utc(13, 0, 0).date_naive();
    let m = Metrics::compute(&l, &hours, &MetricsConfig::new(day, day, 25_000.0)).unwrap();
    let open = &m.open_at_end;
    assert_eq!(open.count, 1);
    assert!(close(
        open.exit_cost,
        (0.000_05 + 0.000_25) * 10_000.0 * 0.8
    ));
    let last = m.equity.last().unwrap();
    assert!(close(last.open_exit_cost, open.exit_cost));
    // The entry's extra half-spread is in the curve's costs.
    assert!(close(last.costs.spread_excess, 1.0 * 0.8));
    assert_eq!(m.trades.costs.spread_excess, 0.0);
}
