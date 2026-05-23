"""Canonical timeframe enum with explicit broker/cache mappings.

Historically tradedesk passed timeframes around as plain strings — ``"1MIN"``,
``"5MINUTE"``, ``"1H"``, ``"1D"``, ``"1440MINUTE"`` — with broker- and
cache-specific aliases (IG REST uses ``"MINUTE_15"``; Dukascopy resamples on
``"15min"``).  The string sprawl bit us once already (RAD-2112: ``1440MINUTE``
was being passed to IG REST verbatim instead of being mapped to ``DAY``).

``Timeframe`` is a :class:`~enum.StrEnum` whose ``value`` is the
**tradedesk-canonical** form (e.g. ``"15MINUTE"``).  Because ``StrEnum`` members
*are* strings, a ``Timeframe`` compares equal to its canonical string and can
be used anywhere a string was previously expected.  Each member knows how to
render itself for the destinations that matter — IG REST resolutions and
pandas resample rules for the Dukascopy cache reader.
"""

from __future__ import annotations

import warnings
from enum import StrEnum
from typing import Final


class Timeframe(StrEnum):
    """Canonical timeframe with broker/cache mappings.

    Member values are tradedesk-canonical strings.  Equality with the
    canonical string holds: ``Timeframe.MINUTE_15 == "15MINUTE"``.

    Use :meth:`Timeframe.from_value` to coerce arbitrary legacy strings
    (``"1MIN"``, ``"60MINUTE"``, ``"HOUR_4"`` …) into a member.
    """

    SECOND = "SECOND"
    MINUTE_1 = "1MINUTE"
    MINUTE_2 = "2MINUTE"
    MINUTE_3 = "3MINUTE"
    MINUTE_5 = "5MINUTE"
    MINUTE_10 = "10MINUTE"
    MINUTE_15 = "15MINUTE"
    MINUTE_30 = "30MINUTE"
    HOUR_1 = "HOUR"
    HOUR_2 = "2HOUR"
    HOUR_3 = "3HOUR"
    HOUR_4 = "4HOUR"
    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"

    # ---------------------------------------------------------------- mappings
    def to_ig_resolution(self) -> str:
        """Render as an IG REST ``resolution`` string."""
        return _IG_RESOLUTION[self]

    def to_dukascopy_rule(self) -> str:
        """Render as a pandas ``resample`` rule for the Dukascopy reader."""
        return _DUKASCOPY_RULE[self]

    def to_seconds(self) -> int:
        """Bar length in seconds.

        Raises:
            ValueError: for calendar timeframes (``MONTH``) whose duration is
                not constant.
        """
        try:
            return _SECONDS[self]
        except KeyError as e:
            raise ValueError(
                f"{self.name} has no fixed second-count; use calendar arithmetic instead."
            ) from e

    # ---------------------------------------------------------------- parsing
    @classmethod
    def from_value(cls, value: "Timeframe | str") -> "Timeframe":
        """Coerce a Timeframe or legacy/alias string to a :class:`Timeframe`.

        Accepts the canonical forms (``"1MINUTE"``, ``"HOUR"``, ``"DAY"`` …),
        the older tradedesk shortforms (``"1MIN"``, ``"15MIN"``, ``"1H"``,
        ``"1D"`` …), the ``NMINUTE`` aliases (``"60MINUTE"`` ≡ ``"HOUR"``,
        ``"240MINUTE"`` ≡ ``"4HOUR"``, ``"1440MINUTE"`` ≡ ``"DAY"``), and the
        IG-native ``MINUTE_N`` / ``HOUR_N`` forms.  Matching is case-insensitive.

        Raises:
            ValueError: if *value* cannot be recognised.
        """
        if isinstance(value, Timeframe):
            return value
        if not isinstance(value, str):
            raise TypeError(
                f"Timeframe value must be a Timeframe or str; got {type(value).__name__}"
            )
        key = value.strip().upper()
        try:
            return _ALIASES[key]
        except KeyError as e:
            raise ValueError(f"Unknown timeframe: {value!r}") from e


def coerce(value: "Timeframe | str", *, source: str | None = None) -> Timeframe:
    """Coerce a value to a :class:`Timeframe`, warning when a bare string was passed.

    Internal helper for the migration window: call sites that accept
    ``Timeframe | str`` use this to resolve the input and emit a one-shot
    :class:`DeprecationWarning` if the caller is still on the string form.
    The warning is silent for inputs that already are :class:`Timeframe`
    members.

    The warning will be removed once ``Timeframe`` is required at the public
    API boundary (planned for the next minor bump after 1.5).
    """
    if isinstance(value, Timeframe):
        return value
    tf = Timeframe.from_value(value)
    where = f" in {source}" if source else ""
    warnings.warn(
        f"Passing timeframe as a bare string ({value!r}){where} is deprecated; "
        f"pass tradedesk.marketdata.Timeframe.{tf.name} instead.",
        DeprecationWarning,
        stacklevel=3,
    )
    return tf


# ---------------------------------------------------------------------- tables

_IG_RESOLUTION: Final[dict[Timeframe, str]] = {
    Timeframe.SECOND: "SECOND",
    Timeframe.MINUTE_1: "MINUTE",
    Timeframe.MINUTE_2: "MINUTE_2",
    Timeframe.MINUTE_3: "MINUTE_3",
    Timeframe.MINUTE_5: "MINUTE_5",
    Timeframe.MINUTE_10: "MINUTE_10",
    Timeframe.MINUTE_15: "MINUTE_15",
    Timeframe.MINUTE_30: "MINUTE_30",
    Timeframe.HOUR_1: "HOUR",
    Timeframe.HOUR_2: "HOUR_2",
    Timeframe.HOUR_3: "HOUR_3",
    Timeframe.HOUR_4: "HOUR_4",
    Timeframe.DAY: "DAY",
    Timeframe.WEEK: "WEEK",
    Timeframe.MONTH: "MONTH",
}

_DUKASCOPY_RULE: Final[dict[Timeframe, str]] = {
    Timeframe.SECOND: "1s",
    Timeframe.MINUTE_1: "1min",
    Timeframe.MINUTE_2: "2min",
    Timeframe.MINUTE_3: "3min",
    Timeframe.MINUTE_5: "5min",
    Timeframe.MINUTE_10: "10min",
    Timeframe.MINUTE_15: "15min",
    Timeframe.MINUTE_30: "30min",
    Timeframe.HOUR_1: "1h",
    Timeframe.HOUR_2: "2h",
    Timeframe.HOUR_3: "3h",
    Timeframe.HOUR_4: "4h",
    Timeframe.DAY: "1D",
    Timeframe.WEEK: "1W",
    Timeframe.MONTH: "1M",
}

_SECONDS: Final[dict[Timeframe, int]] = {
    Timeframe.SECOND: 1,
    Timeframe.MINUTE_1: 60,
    Timeframe.MINUTE_2: 120,
    Timeframe.MINUTE_3: 180,
    Timeframe.MINUTE_5: 300,
    Timeframe.MINUTE_10: 600,
    Timeframe.MINUTE_15: 900,
    Timeframe.MINUTE_30: 1800,
    Timeframe.HOUR_1: 3600,
    Timeframe.HOUR_2: 7200,
    Timeframe.HOUR_3: 10800,
    Timeframe.HOUR_4: 14400,
    Timeframe.DAY: 86400,
    Timeframe.WEEK: 604800,
    # Calendar months are not constant; "30 days" is a convenient approximation
    # but no real call site uses it.  Keep it out of the seconds table so that
    # ``Timeframe.MONTH.to_seconds()`` raises rather than silently lying.
}


def _build_alias_table() -> dict[str, Timeframe]:
    aliases: dict[str, Timeframe] = {}

    # Canonical values (StrEnum value strings).
    for member in Timeframe:
        aliases[member.value.upper()] = member
        aliases[member.name.upper()] = member

    # NMIN / NMINUTE shortforms — derived from the seconds table so we don't
    # repeat each variant by hand.
    for member, secs in _SECONDS.items():
        if secs >= 60 and secs % 60 == 0:
            minutes = secs // 60
            aliases[f"{minutes}MIN"] = member
            aliases[f"{minutes}MINUTE"] = member
        if secs >= 3600 and secs % 3600 == 0:
            hours = secs // 3600
            aliases[f"{hours}H"] = member
            aliases[f"{hours}HOUR"] = member

    # IG-native MINUTE_N / HOUR_N forms.
    for member, ig in _IG_RESOLUTION.items():
        aliases[ig.upper()] = member

    # Common one-letter shortforms.
    aliases["1D"] = Timeframe.DAY
    aliases["1DAY"] = Timeframe.DAY
    aliases["1W"] = Timeframe.WEEK
    aliases["1WEEK"] = Timeframe.WEEK
    aliases["1MONTH"] = Timeframe.MONTH
    aliases["1S"] = Timeframe.SECOND

    return aliases


_ALIASES: Final[dict[str, Timeframe]] = _build_alias_table()


__all__ = [
    "Timeframe",
    "coerce",
]
