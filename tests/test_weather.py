"""날씨 묶음(psd/weather.py)과 시간 자료 전처리(scripts/preprocess_aws_weather.py) 테스트."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from psd import weather as W
from psd.cycle import station_counts
from scripts.preprocess_aws_weather import daily_extra, dew_point, hourly_flags


def _daily(stns, days, rng):
    rows = []
    for stn in stns:
        for d in days:
            rows.append({
                "date": d, "stn": stn,
                "af_hum": rng.uniform(0.5, 2), "cond_hours": rng.integers(0, 6), "rn_hours": rng.integers(0, 5),
                "rn_max": rng.uniform(0, 10), "af_cap": rng.uniform(0.5, 2), "af_sol": rng.uniform(0.5, 2),
                "hot_hours": rng.integers(0, 4), "frz_hours": rng.integers(0, 3), "thaw_count": rng.integers(0, 2),
                "ws_max": rng.uniform(1, 10), "wind_hours": rng.integers(0, 3), "rain_wind_hours": rng.integers(0, 2),
            })
    return pd.DataFrame(rows)


def test_bundle_reference_mean_is_one():
    """동해선 평소 하루 = 1: 지점별 역 수로 가중한 평균이 묶음마다 1이다."""
    rng = np.random.default_rng(0)
    stns = list(station_counts())[:3]
    days = list(pd.date_range("2024-01-01", periods=60).date)
    s = W.stress_from_daily(_daily(stns + ["999"], days, rng))  # 동해선이 안 쓰는 지점은 빠진다
    assert set(s["stn"]) == set(stns)
    w = s["stn"].map(station_counts())
    for b in W.BUNDLES:
        assert np.average(s[b], weights=w) == pytest.approx(1.0)
        assert (s[b] >= 0).all()


def test_component_ratio_capped():
    rng = np.random.default_rng(1)
    stns = list(station_counts())[:2]
    df = _daily(stns, list(pd.date_range("2024-01-01", periods=50).date), rng)
    df.loc[0, "rn_max"] = 10_000  # 말도 안 되는 폭우 하루
    s = W.stress_from_daily(df)
    assert s["r_rn_max"].max() == W.CAP


def test_component_missing_uses_rest():
    rng = np.random.default_rng(2)
    stns = list(station_counts())[:2]
    df = _daily(stns, list(pd.date_range("2024-01-01", periods=30).date), rng)
    df.loc[0, ["frz_hours"]] = np.nan
    df.loc[1, ["frz_hours", "thaw_count"]] = np.nan
    s = W.stress_from_daily(df)
    assert not np.isnan(s.loc[0, "cold"])  # 재료 하나만 결측이면 나머지로
    assert np.isnan(s.loc[1, "cold"])  # 둘 다 결측이면 결측


def test_climatology_constant_series_has_zero_anomaly():
    days = list(pd.date_range("2022-01-01", "2023-12-31").date)
    s = pd.DataFrame({"date": days, "stn": "940", **{b: 1.5 for b in W.BUNDLES}})
    c = W.climatology(s)
    for b in W.BUNDLES:
        assert c[b + "_clim"].to_numpy() == pytest.approx(1.5)
        assert c[b + "_anom"].abs().max() == pytest.approx(0.0)


def test_climatology_wraps_year_end():
    """12월 31일 평년값은 12월 말과 1월 초를 함께 본다(원형 이동 평균)."""
    days = list(pd.date_range("2022-01-01", "2023-12-31").date)
    s = pd.DataFrame({"date": days, "stn": "940"})
    for b in W.BUNDLES:
        s[b] = [3.0 if d.month == 1 else 1.0 for d in days]
    c = W.climatology(s)
    dec31 = c[pd.to_datetime(c["date"]) == "2022-12-31"]["humid_clim"].iloc[0]
    assert 1.0 < dec31 < 3.0


def test_source_day_fills_outside_range():
    first, last = date(2022, 1, 1), date(2025, 12, 31)
    assert W._source_day(date(2026, 7, 15), first, last) == date(2025, 7, 15)
    assert W._source_day(date(2021, 3, 1), first, last) == date(2022, 3, 1)
    assert W._source_day(date(2028, 2, 29), first, last) == date(2025, 2, 28)


def test_real_weather_file():
    """저장소의 날씨 파일로 실제 점수가 만들어진다(묶음 열이 다 있고, 동해선 7개 지점)."""
    s = W.daily_stress()
    assert set(s["stn"]) == set(station_counts())
    for b in W.BUNDLES:
        assert s[b].notna().mean() > 0.99
    month = pd.to_datetime(s["date"]).dt.month
    assert s[month.isin([7, 8])]["humid"].mean() > s[month.isin([1, 2])]["humid"].mean()  # 여름이 더 습하다
    assert s[month.isin([1, 2])]["cold"].mean() > s[month.isin([7, 8])]["cold"].mean()
    f = W.stress_for("940", [date(2025, 8, 1), date(2026, 8, 1)])
    assert list(f["filled"]) == [False, True]
    assert f.loc[0, "humid"] == f.loc[1, "humid"]


# ---------- 시간 자료 → 하루 값 ----------


def _hours(rows):
    df = pd.DataFrame(rows, columns=["datetime", "ta", "hm", "ws", "rn"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    df["date"] = df["datetime"].dt.normalize()
    return df


def test_dew_point_saturated_equals_temperature():
    assert dew_point(pd.Series([20.0]), pd.Series([100.0]))[0] == pytest.approx(20.0, abs=0.01)
    assert dew_point(pd.Series([20.0]), pd.Series([50.0]))[0] == pytest.approx(9.3, abs=0.2)


def test_hourly_flags_and_daily_extra():
    g = _hours([
        ("2024-01-10 00:00", -2.0, 60, 2, 0),
        ("2024-01-10 01:00", 1.0, 60, 9, 0),     # 얼었다 녹음, 강풍
        ("2024-01-10 02:00", -1.0, 60, 3, 0.5),  # 비 + 약한 바람
        ("2024-01-10 03:00", 0.5, 97, 6, 2.0),   # 녹음, 결로(습도 95↑), 비바람
        ("2024-01-10 05:00", 10.0, 90, 1, 0),    # 한 시간 건너뜀 → 이전 기온 없음
        ("2024-01-10 06:00", 20.0, 90, 1, 0),    # 이슬점(약 18℃) > 한 시간 전 10℃ → 결로
        ("2024-01-10 07:00", 31.0, 30, 1, 0),    # 더움, 건조
    ])
    d = daily_extra(hourly_flags(g)).iloc[0]
    assert d["frz_hours"] == 2
    assert d["thaw_count"] == 2
    assert d["wind_hours"] == 1
    assert d["rn_hours"] == 2
    assert d["rain_wind_hours"] == 1
    assert d["rn_sum"] == pytest.approx(2.5)
    assert d["rn_max"] == pytest.approx(2.0)
    assert d["cond_hours"] == 2
    assert d["hot_hours"] == 1
    assert d["dry_hours"] == 1
    assert d["ws_max"] == 9
