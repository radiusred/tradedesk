"""Tests for tradedesk.recording.report – analysis report preparation."""

import pytest

from tradedesk.recording import report

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _metric(
    instrument="USDJPY",
    final_equity="100.0",
    max_dd="-20.0",
    win_rate="60.0",
    profit_factor="1.5",
    expectancy="5.0",
    round_trips="10",
    fills="20",
    avg_hold_min="30.0",
) -> dict[str, str]:
    return {
        "instrument": instrument,
        "final_equity": final_equity,
        "max_dd": max_dd,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "round_trips": round_trips,
        "fills": fills,
        "avg_hold_min": avg_hold_min,
    }


def _rt(
    instrument="USDJPY",
    pnl="10.0",
    exit_ts="2026-01-15T12:00:00Z",
    entry_ts="2026-01-15T11:00:00Z",
    hold_minutes="60",
    direction="long",
    exit_reason="take_profit",
    mfe_pnl="",
    mae_pnl="",
    financing_cost="",
    admin_cost="",
) -> dict[str, str]:
    return {
        "instrument": instrument,
        "pnl": pnl,
        "exit_ts": exit_ts,
        "entry_ts": entry_ts,
        "hold_minutes": hold_minutes,
        "direction": direction,
        "exit_reason": exit_reason,
        "mfe_pnl": mfe_pnl,
        "mae_pnl": mae_pnl,
        "financing_cost": financing_cost,
        "admin_cost": admin_cost,
    }


# ---------------------------------------------------------------------------
# calc_stats
# ---------------------------------------------------------------------------

class TestCalcStats:
    def test_empty_list(self):
        result = report.calc_stats([])
        assert result["count"] == 0
        assert result["mean"] == 0.0
        assert result["std"] == 0.0

    def test_single_value(self):
        result = report.calc_stats([42.0])
        assert result["count"] == 1
        assert result["mean"] == 42.0
        assert result["std"] == 0.0
        assert result["min"] == 42.0
        assert result["max"] == 42.0

    def test_known_values(self):
        result = report.calc_stats([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
        assert result["count"] == 8
        assert result["mean"] == 5.0
        assert result["min"] == 2.0
        assert result["max"] == 9.0
        assert result["std"] == pytest.approx(2.0, abs=0.01)

    def test_negative_values(self):
        result = report.calc_stats([-10.0, -5.0, 0.0, 5.0, 10.0])
        assert result["mean"] == 0.0
        assert result["min"] == -10.0
        assert result["max"] == 10.0


# ---------------------------------------------------------------------------
# standardize
# ---------------------------------------------------------------------------

class TestStandardize:
    def test_empty_list(self):
        assert report.standardize([]) == []

    def test_identical_values(self):
        result = report.standardize([5.0, 5.0, 5.0])
        assert all(v == 0 for v in result)

    def test_known_z_scores(self):
        result = report.standardize([1.0, 3.0])
        assert result[0] == pytest.approx(-1.0)
        assert result[1] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# euclidean_distance
# ---------------------------------------------------------------------------

class TestEuclideanDistance:
    def test_same_point(self):
        p = {"a_z": 1.0, "b_z": 2.0}
        assert report.euclidean_distance(p, p, ["a", "b"]) == 0.0

    def test_known_distance(self):
        p1 = {"x_z": 0.0, "y_z": 0.0}
        p2 = {"x_z": 3.0, "y_z": 4.0}
        assert report.euclidean_distance(p1, p2, ["x", "y"]) == pytest.approx(5.0)

    def test_missing_keys_ignored(self):
        p1 = {"x_z": 1.0}
        p2 = {"x_z": 4.0}
        dist = report.euclidean_distance(p1, p2, ["x", "missing"])
        assert dist == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# _ensure_instrument_field
# ---------------------------------------------------------------------------

class TestEnsureInstrumentField:
    def test_adds_instrument_from_epic(self):
        rows = [{"epic": "IX.D.DAX.IFD.IP"}]
        report._ensure_instrument_field(rows)
        assert rows[0]["instrument"] == "IX.D.DAX.IFD.IP"

    def test_preserves_existing_instrument(self):
        rows = [{"instrument": "USDJPY", "epic": "old"}]
        report._ensure_instrument_field(rows)
        assert rows[0]["instrument"] == "USDJPY"

    def test_noop_without_epic(self):
        rows = [{"instrument": "USDJPY"}]
        report._ensure_instrument_field(rows)
        assert rows[0]["instrument"] == "USDJPY"


# ---------------------------------------------------------------------------
# _prepare_overall_performance
# ---------------------------------------------------------------------------

class TestPrepareOverallPerformance:
    def test_splits_portfolio_and_instruments(self):
        metrics = [
            _metric(instrument="PORTFOLIO", final_equity="500.0"),
            _metric(instrument="AAA", final_equity="300.0"),
            _metric(instrument="BBB", final_equity="200.0"),
        ]
        portfolio, instruments, sorted_inst = report._prepare_overall_performance(metrics)
        assert len(portfolio) == 1
        assert portfolio[0]["instrument"] == "PORTFOLIO"
        assert len(instruments) == 2
        assert sorted_inst[0]["instrument"] == "AAA"
        assert sorted_inst[1]["instrument"] == "BBB"

    def test_empty_metrics(self):
        portfolio, instruments, sorted_inst = report._prepare_overall_performance([])
        assert portfolio == []
        assert instruments == []
        assert sorted_inst == []


# ---------------------------------------------------------------------------
# _prepare_stats_table
# ---------------------------------------------------------------------------

class TestPrepareStatsTable:
    def test_returns_row_per_metric(self):
        metrics = [_metric(instrument="AAA"), _metric(instrument="BBB")]
        table = report._prepare_stats_table(metrics)
        metric_names = [row["metric"] for row in table]
        assert "final_equity" in metric_names
        assert "max_dd" in metric_names
        assert "win_rate" in metric_names
        assert "profit_factor" in metric_names
        assert "expectancy" in metric_names
        assert len(table) == 5

    def test_cv_zero_when_mean_zero(self):
        metrics = [
            _metric(instrument="A", final_equity="10.0"),
            _metric(instrument="B", final_equity="-10.0"),
        ]
        table = report._prepare_stats_table(metrics)
        fe_row = next(r for r in table if r["metric"] == "final_equity")
        assert fe_row["cv"] == 0


# ---------------------------------------------------------------------------
# _prepare_consistency_data
# ---------------------------------------------------------------------------

class TestPrepareConsistencyData:
    def test_basic(self):
        round_trips = [
            _rt(instrument="AAA", pnl="10.0"),
            _rt(instrument="AAA", pnl="-5.0"),
            _rt(instrument="BBB", pnl="3.0"),
            _rt(instrument="BBB", pnl="4.0"),
        ]
        result = report._prepare_consistency_data(round_trips)
        assert len(result) == 2
        names = [r["instrument"] for r in result]
        assert "AAA" in names
        assert "BBB" in names
        for r in result:
            assert "mean_pnl" in r
            assert "std_pnl" in r
            assert "cv_pnl" in r
            assert "outlier_pct" in r

    def test_sorted_by_cv(self):
        round_trips = [
            _rt(instrument="VOLATILE", pnl="100.0"),
            _rt(instrument="VOLATILE", pnl="-90.0"),
            _rt(instrument="STABLE", pnl="5.0"),
            _rt(instrument="STABLE", pnl="6.0"),
        ]
        result = report._prepare_consistency_data(round_trips)
        assert abs(result[0]["cv_pnl"]) <= abs(result[1]["cv_pnl"])


# ---------------------------------------------------------------------------
# _classify_volatility
# ---------------------------------------------------------------------------

class TestClassifyVolatility:
    def test_splits_low_high(self):
        consistency = [
            {"instrument": "LOW", "cv_pnl": 10.0},
            {"instrument": "HIGH", "cv_pnl": 90.0},
        ]
        result = report._classify_volatility(consistency)
        assert "LOW" in result["low"]
        assert "HIGH" in result["high"]

    def test_empty_data(self):
        assert report._classify_volatility([]) == {}


# ---------------------------------------------------------------------------
# _prepare_exposure_data
# ---------------------------------------------------------------------------

class TestPrepareExposureData:
    def test_basic(self):
        metrics = [
            _metric(instrument="AAA", round_trips="20", fills="40"),
            _metric(instrument="BBB", round_trips="10", fills="20"),
        ]
        data, total = report._prepare_exposure_data(metrics)
        assert total == 30
        assert len(data) == 2
        assert data[0]["instrument"] == "AAA"
        assert data[0]["pct_trades"] == pytest.approx(66.67, abs=0.1)

    def test_zero_round_trips(self):
        metrics = [_metric(instrument="AAA", round_trips="0")]
        data, total = report._prepare_exposure_data(metrics)
        assert total == 0
        assert data[0]["pct_trades"] == 0

    def test_drops_portfolio_row(self):
        metrics = [
            _metric(instrument="PORTFOLIO", round_trips="30", fills="60"),
            _metric(instrument="AAA", round_trips="20", fills="40"),
            _metric(instrument="BBB", round_trips="10", fills="20"),
        ]
        data, total = report._prepare_exposure_data(metrics)
        assert total == 30
        assert {row["instrument"] for row in data} == {"AAA", "BBB"}
        assert data[0]["pct_trades"] == pytest.approx(66.67, abs=0.1)


# ---------------------------------------------------------------------------
# _prepare_risk_adj_data
# ---------------------------------------------------------------------------

class TestPrepareRiskAdjData:
    def test_basic(self):
        metrics = [_metric(instrument="AAA", final_equity="100.0", max_dd="-50.0")]
        consistency = [{"instrument": "AAA", "std_pnl": 10.0, "cv_pnl": 50.0}]
        result = report._prepare_risk_adj_data(metrics, consistency)
        assert len(result) == 1
        assert result[0]["return_per_risk"] == pytest.approx(10.0)
        assert result[0]["dd_ratio"] == pytest.approx(2.0)

    def test_zero_std(self):
        metrics = [_metric(instrument="AAA", final_equity="100.0", max_dd="-50.0")]
        consistency = [{"instrument": "AAA", "std_pnl": 0.0, "cv_pnl": 0.0}]
        result = report._prepare_risk_adj_data(metrics, consistency)
        assert result[0]["return_per_risk"] == 0

    def test_zero_drawdown(self):
        metrics = [_metric(instrument="AAA", final_equity="100.0", max_dd="0.0")]
        consistency = [{"instrument": "AAA", "std_pnl": 10.0, "cv_pnl": 50.0}]
        result = report._prepare_risk_adj_data(metrics, consistency)
        assert result[0]["dd_ratio"] == 0


# ---------------------------------------------------------------------------
# _prepare_similarity
# ---------------------------------------------------------------------------

class TestPrepareSimilarity:
    def test_needs_at_least_two_instruments(self):
        metrics = [_metric(instrument="AAA")]
        consistency = [{"instrument": "AAA", "cv_pnl": 10.0}]
        assert report._prepare_similarity(metrics, consistency) == {}

    def test_two_instruments(self):
        metrics = [
            _metric(instrument="AAA", final_equity="100.0", win_rate="60.0",
                    profit_factor="1.5", expectancy="5.0"),
            _metric(instrument="BBB", final_equity="200.0", win_rate="70.0",
                    profit_factor="2.0", expectancy="8.0"),
        ]
        consistency = [
            {"instrument": "AAA", "cv_pnl": 10.0},
            {"instrument": "BBB", "cv_pnl": 20.0},
        ]
        result = report._prepare_similarity(metrics, consistency)
        assert result["min_pair"] is not None
        assert result["max_pair"] is not None
        assert result["min_dist"] >= 0
        assert len(result["matrix_headers"]) == 2
        assert len(result["matrix_rows"]) == 2


# ---------------------------------------------------------------------------
# _prepare_evolution_data
# ---------------------------------------------------------------------------

class TestPrepareEvolutionData:
    def test_basic(self):
        inst_data = {
            "USDJPY": [
                _rt(pnl="10.0", exit_ts="2026-01-15T12:00:00Z"),
                _rt(pnl="-5.0", exit_ts="2026-01-15T13:00:00Z"),
                _rt(pnl="20.0", exit_ts="2026-01-15T14:00:00Z"),
            ]
        }
        result = report._prepare_evolution_data(inst_data)
        assert len(result) == 1
        assert result[0]["instrument"] == "USDJPY"
        assert result[0]["final_pnl"] == pytest.approx(25.0)
        assert result[0]["peak_pnl"] == pytest.approx(25.0)
        assert result[0]["max_dd"] == pytest.approx(-5.0)

    def test_empty_instrument(self):
        result = report._prepare_evolution_data({})
        assert result == []


# ---------------------------------------------------------------------------
# _prepare_insights
# ---------------------------------------------------------------------------

class TestPrepareInsights:
    def test_basic(self):
        metrics = [
            _metric(instrument="AAA", final_equity="200.0", round_trips="15"),
            _metric(instrument="BBB", final_equity="50.0", round_trips="5"),
        ]
        consistency = [
            {"instrument": "AAA", "cv_pnl": 10.0},
            {"instrument": "BBB", "cv_pnl": 50.0},
        ]
        risk_adj = [
            {"instrument": "AAA", "return_per_risk": 10.0, "dd_ratio": 2.0},
            {"instrument": "BBB", "return_per_risk": 5.0, "dd_ratio": 1.0},
        ]
        result = report._prepare_insights(metrics, consistency, risk_adj, total_round_trips=20)
        assert result["best"]["instrument"] == "AAA"
        assert result["worst"]["instrument"] == "BBB"
        assert result["range"] == pytest.approx(150.0)
        assert "consistent" in result
        assert "risk" in result
        assert "exposure" in result

    def test_empty_metrics(self):
        assert report._prepare_insights([], [], [], 0) == {}

    def test_excludes_portfolio(self):
        metrics = [
            _metric(instrument="PORTFOLIO", final_equity="1000.0", round_trips="30"),
            _metric(instrument="AAA", final_equity="200.0", round_trips="15"),
        ]
        result = report._prepare_insights(metrics, [], [], 15)
        assert result["best"]["instrument"] == "AAA"


# ---------------------------------------------------------------------------
# _prepare_additional_analysis
# ---------------------------------------------------------------------------

class TestPrepareAdditionalAnalysis:
    def test_basic(self):
        rt_rows = [
            _rt(instrument="AAA", pnl="10.0", exit_ts="2026-01-15T12:00:00Z",
                entry_ts="2026-01-15T11:00:00Z", hold_minutes="60", direction="long"),
            _rt(instrument="AAA", pnl="-5.0", exit_ts="2026-01-15T13:00:00Z",
                entry_ts="2026-01-15T12:00:00Z", hold_minutes="60", direction="short"),
            _rt(instrument="AAA", pnl="8.0", exit_ts="2026-01-15T14:00:00Z",
                entry_ts="2026-01-15T13:00:00Z", hold_minutes="60", direction="long"),
        ]
        inst_data = {"AAA": rt_rows}
        (win_loss, streaks, hold_time, best_worst,
         mfe_mae, long_short) = report._prepare_additional_analysis(inst_data, rt_rows)

        assert len(win_loss) == 1
        assert win_loss[0]["wins"] == 2
        assert win_loss[0]["losses"] == 1

        assert len(streaks) == 1
        assert streaks[0]["max_win"] == 1
        assert streaks[0]["max_loss"] == 1

        assert len(hold_time) == 1
        assert hold_time[0]["avg"] == 60.0

        assert len(best_worst) == 1
        assert best_worst[0]["best"]["pnl"] == 10.0
        assert best_worst[0]["worst"]["pnl"] == -5.0

        assert len(long_short) == 1
        assert long_short[0]["long_cnt"] == 2
        assert long_short[0]["short_cnt"] == 1

    def test_mfe_mae_when_present(self):
        rt_rows = [
            _rt(instrument="AAA", pnl="10.0", mfe_pnl="15.0", mae_pnl="-3.0",
                exit_ts="2026-01-15T12:00:00Z", entry_ts="2026-01-15T11:00:00Z"),
        ]
        inst_data = {"AAA": rt_rows}
        _, _, _, _, mfe_mae, _ = report._prepare_additional_analysis(inst_data, rt_rows)
        assert len(mfe_mae) == 1
        assert mfe_mae[0]["avg_mfe"] == pytest.approx(15.0)
        assert mfe_mae[0]["avg_mae"] == pytest.approx(-3.0)

    def test_mfe_mae_absent(self):
        rt_rows = [
            _rt(instrument="AAA", pnl="10.0",
                exit_ts="2026-01-15T12:00:00Z", entry_ts="2026-01-15T11:00:00Z"),
        ]
        inst_data = {"AAA": rt_rows}
        _, _, _, _, mfe_mae, _ = report._prepare_additional_analysis(inst_data, rt_rows)
        assert len(mfe_mae) == 0

    def test_long_short_bias_neutral(self):
        rt_rows = [
            _rt(instrument="AAA", pnl="10.0", direction="long",
                exit_ts="2026-01-15T12:00:00Z", entry_ts="2026-01-15T11:00:00Z"),
            _rt(instrument="AAA", pnl="10.0", direction="short",
                exit_ts="2026-01-15T13:00:00Z", entry_ts="2026-01-15T12:00:00Z"),
        ]
        inst_data = {"AAA": rt_rows}
        _, _, _, _, _, long_short = report._prepare_additional_analysis(inst_data, rt_rows)
        assert long_short[0]["bias"] == "Neutral"


# ---------------------------------------------------------------------------
# _prepare_monthly_data
# ---------------------------------------------------------------------------

class TestPrepareMonthlyData:
    def test_values_are_iterable(self):
        rt_rows = [
            _rt(instrument="com.example.inst1", pnl="10.50",
                exit_ts="2026-01-15T12:00:00Z"),
            _rt(instrument="com.example.inst1", pnl="-5.25",
                exit_ts="2026-01-20T12:00:00Z"),
        ]
        inst_data = {"com.example.inst1": rt_rows}
        monthly = report._prepare_monthly_data(rt_rows, inst_data)

        assert isinstance(monthly.rows, list)
        assert len(monthly.rows) >= 1
        row = monthly.rows[0]
        assert isinstance(row.values, list)
        assert all(isinstance(v, str) for v in row.values)
        assert isinstance(monthly.totals.values, list)

    def test_multiple_months(self):
        rt_rows = [
            _rt(instrument="AAA", pnl="10.0", exit_ts="2026-01-15T12:00:00Z"),
            _rt(instrument="AAA", pnl="20.0", exit_ts="2026-02-15T12:00:00Z"),
        ]
        inst_data = {"AAA": rt_rows}
        monthly = report._prepare_monthly_data(rt_rows, inst_data)
        assert len(monthly.rows) == 2

    def test_grand_total(self):
        rt_rows = [
            _rt(instrument="AAA", pnl="10.0", exit_ts="2026-01-15T12:00:00Z"),
            _rt(instrument="AAA", pnl="20.0", exit_ts="2026-02-15T12:00:00Z"),
        ]
        inst_data = {"AAA": rt_rows}
        monthly = report._prepare_monthly_data(rt_rows, inst_data)
        assert monthly.totals.grand == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# _prepare_exit_reasons
# ---------------------------------------------------------------------------

class TestPrepareExitReasons:
    def test_no_exit_reasons(self):
        rt_rows = [_rt(exit_reason="")]
        inst_data = {"USDJPY": rt_rows}
        result = report._prepare_exit_reasons(rt_rows, inst_data)
        assert result["available"] is False
        assert result["portfolio"] == []

    def test_with_exit_reasons(self):
        rt_rows = [
            _rt(instrument="AAA", pnl="10.0", exit_reason="take_profit"),
            _rt(instrument="AAA", pnl="-5.0", exit_reason="stop_loss"),
            _rt(instrument="AAA", pnl="3.0", exit_reason="take_profit"),
        ]
        inst_data = {"AAA": rt_rows}
        result = report._prepare_exit_reasons(rt_rows, inst_data)
        assert result["available"] is True
        assert len(result["portfolio"]) == 2
        reasons = {r["reason"] for r in result["portfolio"]}
        assert "take_profit" in reasons
        assert "stop_loss" in reasons
        assert len(result["instruments"]) == 1


# ---------------------------------------------------------------------------
# _prepare_financing_summary
# ---------------------------------------------------------------------------

class TestPrepareFinancingSummary:
    def test_no_costs(self):
        rt_rows = [_rt()]
        result = report._prepare_financing_summary(rt_rows)
        assert result["available"] is False
        assert result["total"] == 0.0

    def test_with_costs(self):
        rt_rows = [
            _rt(instrument="AAA", financing_cost="-2.50", admin_cost="-1.00"),
            _rt(instrument="BBB", financing_cost="-1.00", admin_cost="0"),
        ]
        result = report._prepare_financing_summary(rt_rows)
        assert result["available"] is True
        assert result["total_financing"] == pytest.approx(-3.50)
        assert result["total_admin"] == pytest.approx(-1.00)
        assert result["total"] == pytest.approx(-4.50)
        assert len(result["instruments"]) == 2


# ---------------------------------------------------------------------------
# _build_instrument_data
# ---------------------------------------------------------------------------

class TestBuildInstrumentData:
    def test_groups_by_instrument(self):
        rt_rows = [
            _rt(instrument="AAA"),
            _rt(instrument="BBB"),
            _rt(instrument="AAA"),
        ]
        result = report._build_instrument_data(rt_rows)
        assert len(result["AAA"]) == 2
        assert len(result["BBB"]) == 1
