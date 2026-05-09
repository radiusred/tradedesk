"""Wires every report section together and renders the analysis markdown."""

from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .additional import _prepare_additional_analysis
from .consistency import _classify_volatility, _prepare_consistency_data
from .csv_io import _ensure_instrument_field, _read_csv
from .evolution import _build_instrument_data, _prepare_evolution_data
from .exit_reasons import _prepare_exit_reasons
from .exposure import _prepare_exposure_data
from .financing import _prepare_financing_summary
from .graphs import _prepare_graphs
from .insights import _prepare_insights
from .monthly import _prepare_monthly_data
from .overall import _prepare_overall_performance, _prepare_stats_table
from .risk_adj import _prepare_risk_adj_data
from .similarity import _prepare_similarity

_TEMPLATE_DIR = Path(__file__).parent.parent
_TEMPLATE_NAME = "report-template.j2"


def generate_analysis_report(
    output_dir: str | Path,
    with_graphs: bool = True,
    cache_dir: Path | None = None,
) -> None:
    output_path = Path(output_dir)
    metrics_file = output_path / "metrics.csv"
    round_trips_file = output_path / "round_trips.csv"
    equity_file = output_path / "equity.csv"
    equity_daily_file = output_path / "equity_daily.csv"
    report_file = output_path / "analysis.md"

    metrics = _read_csv(metrics_file)
    _ensure_instrument_field(metrics)
    round_trips = _read_csv(round_trips_file)
    _ensure_instrument_field(round_trips)
    equity_daily = _read_csv(equity_daily_file)

    portfolio_metrics, instrument_metrics, sorted_instruments = _prepare_overall_performance(
        metrics
    )
    stats_table = _prepare_stats_table(metrics)
    consistency_data = _prepare_consistency_data(round_trips)
    volatility = _classify_volatility(consistency_data)
    exposure_data, total_round_trips = _prepare_exposure_data(metrics)
    risk_adj_data = _prepare_risk_adj_data(metrics, consistency_data)
    similarity = _prepare_similarity(metrics, consistency_data)
    inst_data = _build_instrument_data(round_trips)
    evolution_data = _prepare_evolution_data(inst_data)
    insights = _prepare_insights(metrics, consistency_data, risk_adj_data, total_round_trips)
    win_loss_data, streak_data, hold_time_data, best_worst_trades, mfe_mae_data, long_short_data = (
        _prepare_additional_analysis(inst_data, round_trips)
    )
    monthly_data = _prepare_monthly_data(round_trips, inst_data)
    exit_reasons = _prepare_exit_reasons(round_trips, inst_data)
    financing_summary = _prepare_financing_summary(round_trips)

    if with_graphs:
        _prepare_graphs(round_trips_file, equity_file, cache_dir=cache_dir)

    context = {
        "portfolio_metrics": portfolio_metrics,
        "instrument_metrics": sorted_instruments,
        "stats_table": stats_table,
        "consistency_data": consistency_data,
        "volatility": volatility,
        "exposure_data": exposure_data,
        "risk_adj_data": risk_adj_data,
        "similarity": similarity,
        "evolution_data": evolution_data,
        "insights": insights,
        "win_loss_data": win_loss_data,
        "streak_data": streak_data,
        "monthly_data": monthly_data,
        "hold_time_data": hold_time_data,
        "best_worst_trades": best_worst_trades,
        "mfe_mae_data": mfe_mae_data,
        "long_short_data": long_short_data,
        "exit_reasons": exit_reasons,
        "financing_summary": financing_summary,
        "equity_daily": equity_daily,
        "report_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "output_path": output_path,
        "graphs": with_graphs,
    }

    env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)))
    template = env.get_template(_TEMPLATE_NAME)
    rendered = template.render(**context)

    with open(report_file, "w") as f:
        f.write(rendered)
