# Label engineering (`tradedesk.ml.labels`)

Phase 6 (RAD-896) — supervised labels for the XGBoost direction
classifier. Two label families and a class-balance reporter.

## Forward-return binary labels

```python
from tradedesk.ml import LabelConfig, forward_return_labels

y = forward_return_labels(
    bars,
    LabelConfig(horizon=5, neutral_band=0.0001, spread_aware=True),
)
```

* `horizon` — bars to look ahead.
* `neutral_band` — absolute return threshold below which the label is `0`
  (simple-return units, e.g. `0.0001` = 1bp).
* `spread_aware` — when `True`, the label uses an ask-to-bid round trip:
  long return is `bid_close[t+h] / ask_close[t] - 1`, short return is
  `bid_close[t] / ask_close[t+h] - 1`. Requires `bid_close` and
  `ask_close` columns.

Returns a nullable `Int8` `pandas.Series` with values in `{-1, 0, 1}`,
trailing `horizon` rows are `NaN`.

## Triple-barrier labels

```python
from tradedesk.ml import TripleBarrierConfig, triple_barrier_labels

out = triple_barrier_labels(
    bars,
    TripleBarrierConfig(horizon=30, atr_period=14, barrier_mult=2.0),
)
out.head()
```

For each bar `t`, three barriers bracket the entry:

* upper target = `close_t + barrier_mult * ATR_t`
* lower stop  = `close_t - barrier_mult * ATR_t`
* vertical barrier `horizon` bars in the future

The first barrier touched within `[t+1, t+horizon]` decides the label:
`+1` upper, `-1` lower, vertical barrier labels by the sign of the
close-to-close return at `t + horizon` (zero inside `vertical_band`).

Output columns:

| column        | dtype       | semantics                                         |
| ------------- | ----------- | ------------------------------------------------- |
| `label`       | `Int8` (NA) | `-1`, `0`, `+1`; NA during ATR warmup or tail     |
| `exit_offset` | `Int32` (NA) | bars from entry to exit (`1..horizon`)           |
| `barrier`     | `string`    | `upper` / `lower` / `vertical` / `ambiguous` / `warmup` |

`ambiguous` covers the case where both barriers are touched within the
*same* bar (intra-bar order unknown); the row is labelled `0`.

`barrier_mult` defaults to symmetric stop/target. Use `upper_mult` /
`lower_mult` for asymmetric barriers.

## Class-balance reporter

`class_balance_report({"fold_name": labels_series, ...})` returns a
`DataFrame` with one row per fold and the columns:

* `n` — non-NA sample count
* `count_<c>` — count for each class in `(-1, 0, 1)`
* `prop_<c>` — proportion (`count_<c> / n`); `0.0` when `n == 0`

Column order is stable: `n`, all `count_*`, then all `prop_*` in the
order of the supplied `classes` argument.

`print_class_balance(...)` does the same and additionally prints a
4-decimal-formatted table to stdout for log inspection during
walk-forward CV. Use it on a per-fold mapping returned by the CV harness
(RAD-900) to spot degenerate-class folds before training.

```python
from tradedesk.ml import print_class_balance

print_class_balance({
    "fold_2024H1": y_train_h1,
    "fold_2024H2": y_train_h2,
    "fold_2025H1": y_train_h1_2025,
})
#               n  count_-1  count_0  count_1  prop_-1  prop_0  prop_1
# fold
# fold_2024H1  ...
# fold_2024H2  ...
# fold_2025H1  ...
```

## No-look-ahead invariant

Both label families read **only** bars at index `>= t` for the entry
side and bars at indices `t+1 .. t+horizon` for the forward side. The
trailing `horizon` rows are explicitly NA. The leakage gate
(RAD-900 / Comp 4) verifies that a deliberately-leaky feature paired
with these labels collapses OOS — confirmation that the labels do not
themselves leak.
