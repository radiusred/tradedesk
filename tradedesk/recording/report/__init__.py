"""Performance analysis report generation for backtest results.

The implementation is split per report section under this package; this module
exposes the public ``generate_analysis_report`` orchestrator plus every helper
referenced by ``tests/recording/test_report.py``.
"""

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
from .orchestrator import generate_analysis_report
from .overall import _prepare_overall_performance, _prepare_stats_table
from .risk_adj import _prepare_risk_adj_data
from .similarity import _prepare_similarity
from .stats import calc_stats, euclidean_distance, standardize

__all__ = [
    "generate_analysis_report",
    "calc_stats",
    "standardize",
    "euclidean_distance",
    "_read_csv",
    "_ensure_instrument_field",
    "_prepare_overall_performance",
    "_prepare_stats_table",
    "_prepare_consistency_data",
    "_classify_volatility",
    "_prepare_exposure_data",
    "_prepare_risk_adj_data",
    "_prepare_similarity",
    "_build_instrument_data",
    "_prepare_evolution_data",
    "_prepare_insights",
    "_prepare_additional_analysis",
    "_prepare_monthly_data",
    "_prepare_exit_reasons",
    "_prepare_financing_summary",
    "_prepare_graphs",
]
