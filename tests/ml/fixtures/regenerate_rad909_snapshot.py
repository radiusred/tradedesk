"""Regenerate ``rad909_feature_snapshot.npz`` for the feature-snapshot drift guard.

Run from the repo root::

    uv run python tests/ml/fixtures/regenerate_rad909_snapshot.py

Only run this when an *intentional* feature change updates the FeatureBuilder
contract. The companion test (``test_indicator_stack_matches_pre_refactor_
snapshot`` in ``tests/ml/test_features.py``) compares with ``rtol=1e-7`` to
absorb cross-Python-build FP noise.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from tests.ml.test_features import _bars
from tradedesk.ml import FeatureBuilder, FeatureConfig


def main() -> None:
    bars = _bars(600, with_bid_ask=True, seed=42)
    out = FeatureBuilder(config=FeatureConfig(drop_warmup=False)).transform(bars)
    arr = out.to_numpy(dtype=np.float64, na_value=np.nan)
    columns = np.array(list(out.columns))
    target = Path(__file__).parent / "rad909_feature_snapshot.npz"
    np.savez_compressed(target, arr=arr, columns=columns)
    print(f"wrote {target} shape={arr.shape}")


if __name__ == "__main__":
    main()
