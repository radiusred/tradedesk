from tradedesk.types import Candle

# ---------------------------------------------------------------------------
# Candle timestamp helpers
# ---------------------------------------------------------------------------


class TestCandleWithMsTimestamp:
    def test_iso_to_ms(self):
        c = Candle(
            timestamp="2025-01-15T12:30:00Z", open=1.0, high=2.0, low=0.5, close=1.5
        )
        result = c.candle_with_ms_timestamp()
        assert isinstance(result.timestamp, int)
        assert isinstance(c.timestamp, str)

    def test_already_int(self):
        c = Candle(timestamp=1736899200000, open=1.0, high=2.0, low=0.5, close=1.5)
        result = c.candle_with_ms_timestamp()
        assert result.timestamp == 1736899200000

    def test_float_to_int(self):
        c = Candle(timestamp=1736899200000.0, open=1.0, high=2.0, low=0.5, close=1.5)
        result = c.candle_with_ms_timestamp()
        assert isinstance(result.timestamp, int)

    def test_numeric_string_to_ms(self):
        """IG Lightstreamer UTM field returns epoch-ms as a string."""
        c = Candle(timestamp="1736899200000", open=1.0, high=2.0, low=0.5, close=1.5)
        result = c.candle_with_ms_timestamp()
        assert isinstance(result.timestamp, int)
        assert result.timestamp == 1736899200000


class TestCandleWithIsoTimestamp:
    def test_ms_int_to_iso(self):
        c = Candle(timestamp=1736899200000, open=1.0, high=2.0, low=0.5, close=1.5)
        result = c.candle_with_iso_timestamp()
        assert isinstance(result.timestamp, str)
        assert "2025-01-15" in result.timestamp

    def test_already_iso(self):
        c = Candle(
            timestamp="2025-01-15T12:30:00Z", open=1.0, high=2.0, low=0.5, close=1.5
        )
        result = c.candle_with_iso_timestamp()
        assert result.timestamp == "2025-01-15T12:30:00Z"

    def test_numeric_string_to_iso(self):
        c = Candle(timestamp="1736899200000", open=1.0, high=2.0, low=0.5, close=1.5)
        result = c.candle_with_iso_timestamp()
        assert isinstance(result.timestamp, str)
        assert "2025-01-15" in result.timestamp

    def test_unexpected_type_best_effort(self):
        """Non-str, non-int type falls through to the best-effort branch."""
        c = Candle(timestamp=1736899200000.0, open=1.0, high=2.0, low=0.5, close=1.5)
        # float is not str and not int – hits the last branch
        result = c.candle_with_iso_timestamp()
        assert isinstance(result.timestamp, str)
