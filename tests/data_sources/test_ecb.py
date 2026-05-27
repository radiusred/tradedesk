"""Tests for the ECB data source."""

from __future__ import annotations

from datetime import date

import pandas as pd

from tradedesk.data_sources import ecb

# Trimmed ECB csvdata response (YC daily): only the columns we read matter,
# but we keep the wide header to prove the parser ignores the extras.
_YC_SAMPLE = (
    "KEY,FREQ,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
    "YC...SR_2Y,B,2026-05-04,2.6404,A\n"
    "YC...SR_2Y,B,2026-05-05,2.6391,A\n"
    "YC...SR_2Y,B,2026-05-06,,A\n"  # missing value dropped
)

# Monthly Euribor sample: TIME_PERIOD is YYYY-MM.
_FM_SAMPLE = (
    "KEY,FREQ,TIME_PERIOD,OBS_VALUE\n"
    "FM...EURIBOR3MD_,M,2026-03,2.20\n"
    "FM...EURIBOR3MD_,M,2026-04,2.17\n"
)


def test_parse_daily_yc():
    df = ecb.parse_ecb_csv(_YC_SAMPLE)
    assert list(df.columns) == ["value"]
    assert df.index.name == "date"
    assert len(df) == 2  # blank OBS_VALUE row dropped
    assert df.index[0] == pd.Timestamp("2026-05-04")
    assert df["value"].tolist() == [2.6404, 2.6391]


def test_parse_monthly_anchors_to_first_of_month():
    df = ecb.parse_ecb_csv(_FM_SAMPLE)
    assert df.index.tolist() == [pd.Timestamp("2026-03-01"), pd.Timestamp("2026-04-01")]
    assert df["value"].tolist() == [2.20, 2.17]


def test_parse_empty_without_obs_value_column():
    df = ecb.parse_ecb_csv("KEY,FREQ,TIME_PERIOD\nx,B,2026-01-01\n")
    assert df.empty


def test_period_parser_handles_all_granularities():
    assert ecb._parse_period("2026-05-04") == pd.Timestamp("2026-05-04")
    assert ecb._parse_period("2026-05") == pd.Timestamp("2026-05-01")
    assert ecb._parse_period("2026") == pd.Timestamp("2026-01-01")


def test_url_uses_flow_key_and_csvdata():
    ser = ecb.DEFAULT_ECB_SERIES["EUR_YLD_2Y"]
    url = ecb._ecb_csv_url(ser, date(2010, 1, 1))
    assert url == (
        f"{ecb.ECB_BASE_URL}/YC/{ser.key}?format=csvdata&startPeriod=2010-01-01"
    )


def test_default_series_cover_estr_yields_and_euribor():
    assert "EUR_ESTR" in ecb.DEFAULT_ECB_SERIES
    assert "EUR_YLD_2Y" in ecb.DEFAULT_ECB_SERIES
    assert "EUR_EURIBOR_3M" in ecb.DEFAULT_ECB_SERIES
    # €STR comes from the EST flow; yields from YC; Euribor from FM.
    assert ecb.DEFAULT_ECB_SERIES["EUR_ESTR"].flow == "EST"
    assert ecb.DEFAULT_ECB_SERIES["EUR_YLD_2Y"].flow == "YC"
    assert ecb.DEFAULT_ECB_SERIES["EUR_EURIBOR_3M"].flow == "FM"
