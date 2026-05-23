"""Unit tests for the ``Timeframe`` enum."""
from __future__ import annotations

import warnings

import pytest

from tradedesk.marketdata.timeframe import Timeframe, coerce


class TestCanonicalValues:
    def test_str_subclass(self) -> None:
        # StrEnum members ARE strings, so canonical equality must hold.
        assert Timeframe.MINUTE_15 == "15MINUTE"
        assert Timeframe.HOUR_1 == "HOUR"
        assert Timeframe.DAY == "DAY"
        assert isinstance(Timeframe.MINUTE_15, str)

    def test_format_interpolates_canonical_string(self) -> None:
        # Used by Lightstreamer item names: f"CHART:{instrument}:{period}".
        assert f"CHART:EPIC:{Timeframe.MINUTE_5}" == "CHART:EPIC:5MINUTE"
        assert f"CHART:EPIC:{Timeframe.DAY}" == "CHART:EPIC:DAY"


class TestFromValue:
    @pytest.mark.parametrize(
        ("alias", "expected"),
        [
            # Canonical forms
            ("1MINUTE", Timeframe.MINUTE_1),
            ("5MINUTE", Timeframe.MINUTE_5),
            ("15MINUTE", Timeframe.MINUTE_15),
            ("30MINUTE", Timeframe.MINUTE_30),
            ("HOUR", Timeframe.HOUR_1),
            ("4HOUR", Timeframe.HOUR_4),
            ("DAY", Timeframe.DAY),
            ("WEEK", Timeframe.WEEK),
            ("MONTH", Timeframe.MONTH),
            # NMINUTE alias forms (RAD-2112 — 1440MINUTE must map to DAY).
            ("60MINUTE", Timeframe.HOUR_1),
            ("120MINUTE", Timeframe.HOUR_2),
            ("240MINUTE", Timeframe.HOUR_4),
            ("1440MINUTE", Timeframe.DAY),
            # Older shortforms.
            ("1MIN", Timeframe.MINUTE_1),
            ("5MIN", Timeframe.MINUTE_5),
            ("15MIN", Timeframe.MINUTE_15),
            ("1H", Timeframe.HOUR_1),
            ("4H", Timeframe.HOUR_4),
            ("1D", Timeframe.DAY),
            ("1W", Timeframe.WEEK),
            # IG-native passthrough.
            ("MINUTE", Timeframe.MINUTE_1),
            ("MINUTE_5", Timeframe.MINUTE_5),
            ("MINUTE_15", Timeframe.MINUTE_15),
            ("MINUTE_30", Timeframe.MINUTE_30),
            ("HOUR_2", Timeframe.HOUR_2),
            ("HOUR_4", Timeframe.HOUR_4),
            # Enum-name lookup.
            ("MINUTE_1", Timeframe.MINUTE_1),
            ("HOUR_1", Timeframe.HOUR_1),
        ],
    )
    def test_recognised_aliases(self, alias: str, expected: Timeframe) -> None:
        assert Timeframe.from_value(alias) is expected

    def test_case_insensitive(self) -> None:
        assert Timeframe.from_value("1minute") is Timeframe.MINUTE_1
        assert Timeframe.from_value("4hour") is Timeframe.HOUR_4

    def test_strips_whitespace(self) -> None:
        assert Timeframe.from_value("  15MINUTE  ") is Timeframe.MINUTE_15

    def test_passthrough_for_existing_member(self) -> None:
        assert Timeframe.from_value(Timeframe.MINUTE_15) is Timeframe.MINUTE_15

    def test_rejects_unknown_string(self) -> None:
        with pytest.raises(ValueError, match="Unknown timeframe"):
            Timeframe.from_value("FOO")

    def test_rejects_non_string(self) -> None:
        with pytest.raises(TypeError):
            Timeframe.from_value(15)  # type: ignore[arg-type]


class TestIGResolution:
    def test_standard_mappings(self) -> None:
        # Mirrors the IGMetadataCache._PERIOD_MAP contract.
        assert Timeframe.MINUTE_1.to_ig_resolution() == "MINUTE"
        assert Timeframe.MINUTE_5.to_ig_resolution() == "MINUTE_5"
        assert Timeframe.MINUTE_15.to_ig_resolution() == "MINUTE_15"
        assert Timeframe.MINUTE_30.to_ig_resolution() == "MINUTE_30"
        assert Timeframe.HOUR_1.to_ig_resolution() == "HOUR"
        assert Timeframe.HOUR_4.to_ig_resolution() == "HOUR_4"
        assert Timeframe.DAY.to_ig_resolution() == "DAY"
        assert Timeframe.WEEK.to_ig_resolution() == "WEEK"
        assert Timeframe.MONTH.to_ig_resolution() == "MONTH"

    def test_rad_2112_regression(self) -> None:
        """``1440MINUTE`` must map to IG REST ``DAY`` (RAD-2112)."""
        assert Timeframe.from_value("1440MINUTE").to_ig_resolution() == "DAY"

    def test_every_member_has_mapping(self) -> None:
        for tf in Timeframe:
            assert tf.to_ig_resolution()  # no KeyError


class TestDukascopyRule:
    @pytest.mark.parametrize(
        ("member", "rule"),
        [
            (Timeframe.MINUTE_1, "1min"),
            (Timeframe.MINUTE_5, "5min"),
            (Timeframe.MINUTE_15, "15min"),
            (Timeframe.HOUR_1, "1h"),
            (Timeframe.HOUR_4, "4h"),
            (Timeframe.DAY, "1D"),
            (Timeframe.WEEK, "1W"),
        ],
    )
    def test_rule(self, member: Timeframe, rule: str) -> None:
        assert member.to_dukascopy_rule() == rule


class TestToSeconds:
    @pytest.mark.parametrize(
        ("member", "secs"),
        [
            (Timeframe.SECOND, 1),
            (Timeframe.MINUTE_1, 60),
            (Timeframe.MINUTE_5, 300),
            (Timeframe.MINUTE_15, 900),
            (Timeframe.HOUR_1, 3600),
            (Timeframe.HOUR_4, 14400),
            (Timeframe.DAY, 86400),
            (Timeframe.WEEK, 604800),
        ],
    )
    def test_known_durations(self, member: Timeframe, secs: int) -> None:
        assert member.to_seconds() == secs

    def test_month_raises(self) -> None:
        with pytest.raises(ValueError, match="MONTH"):
            Timeframe.MONTH.to_seconds()


class TestCoerce:
    def test_member_passthrough_no_warning(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = coerce(Timeframe.MINUTE_15)
        assert result is Timeframe.MINUTE_15

    def test_string_emits_deprecation_warning(self) -> None:
        with pytest.warns(DeprecationWarning, match="deprecated"):
            result = coerce("15MINUTE")
        assert result is Timeframe.MINUTE_15

    def test_warning_mentions_source_when_given(self) -> None:
        with pytest.warns(DeprecationWarning, match="ChartSubscription"):
            coerce("HOUR", source="ChartSubscription")

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError):
            coerce("not-a-timeframe")
