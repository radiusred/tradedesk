"""Module-level constants for ML hyperparameters used across tradedesk.ml.

These constants were previously embedded as function-default arguments, making
them invisible to callers and hard to tune.  Promoting them here gives a single
location to inspect and override across the codebase.

Conventions
-----------
* ``Final`` annotation prevents accidental reassignment.
* Trailing comments state the unit (dimensionless ratio, seconds, …) and the
  file/function that originated the value.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Leakage-sanity check (tradedesk.ml.reporting.run_leakage_sanity)
# ---------------------------------------------------------------------------

# Fraction of Gaussian noise added to the perfect-leak feature so the
# classifier cannot memorise the exact label; keeps accuracy < 1.0 while
# still detecting any genuine future-information leak.
# Dimensionless ratio.  Source: tradedesk/ml/reporting.py::run_leakage_sanity.
LEAKAGE_SANITY_LEAK_NOISE: Final[float] = 0.05

# Minimum per-fold accuracy and AUC the leakage harness must observe to pass.
# A value below this threshold on a synthetic perfect-leak dataset indicates
# the harness itself has regressed.
# Dimensionless ratio in [0, 1].  Source: tradedesk/ml/reporting.py::run_leakage_sanity.
LEAKAGE_SANITY_THRESHOLD_ACCURACY: Final[float] = 0.95

# ---------------------------------------------------------------------------
# Portfolio watchdog (tradedesk.portfolio.base.BasePortfolio)
# ---------------------------------------------------------------------------

# Maximum seconds of silence allowed between portfolio update ticks before the
# watchdog considers the feed stale and takes corrective action.
# Unit: seconds.  Source: tradedesk/portfolio/base.py::BasePortfolio.__init__.
PORTFOLIO_WATCHDOG_THRESHOLD_S: Final[float] = 60.0
