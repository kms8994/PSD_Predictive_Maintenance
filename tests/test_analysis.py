"""역별·시기별 고장 분석(psd/analysis.py) 테스트 — DB 없이, 효과를 심은 가짜 패널로."""
from datetime import date, timedelta

import numpy as np
import pytest

from psd import analysis as A
from psd.weather import BUNDLES


def test_mh_rate_ratio_recovers_planted_ratio():
    rng = np.random.default_rng(0)
    n = 20000
    strata = rng.integers(0, 200, n)
    E = rng.uniform(0.5, 2, n) * (1 + strata % 3)  # 층마다 기준이 다름
    X = rng.random(n) < 0.3
    O = rng.poisson(E * np.where(X, 1.5, 1.0))
    r = A.mh_rate_ratio(O, E, X, strata)
    assert r["lo"] < 1.5 < r["hi"]
    assert r["rr"] == pytest.approx(1.5, rel=0.05)
    none = A.mh_rate_ratio(rng.poisson(E), E, X, strata)
    assert none["lo"] < 1 < none["hi"] or abs(none["rr"] - 1) < 0.05


def test_mh_rate_ratio_ignores_strata_without_contrast():
    O = np.array([5, 5, 3, 3])
    E = np.array([1.0, 1.0, 1.0, 1.0])
    X = np.array([True, True, True, False])
    strata = np.array([0, 0, 1, 1])  # 층 0은 조건 날만 있어 비교가 안 된다
    r = A.mh_rate_ratio(O, E, X, strata)
    assert r["strata"] == 1 and r["rr"] == pytest.approx(1.0)


def test_gamma_ci_and_shrink():
    m, lo, hi = A.gamma_ci(30, 20)
    assert m == pytest.approx(1.5) and lo < 1.5 < hi
    m2, lo2, hi2 = A.gamma_ci(30, 20, k=20)  # 당기면 1 쪽으로
    assert 1.0 < m2 < m and hi2 - lo2 < hi - lo
    exp = np.full(30, 100.0)
    assert A.shrink_strength(np.random.default_rng(1).poisson(exp), exp) >= 100  # 진짜 차이가 없으면 세게 당긴다
    obs = exp * np.r_[np.full(15, 0.7), np.full(15, 1.3)]
    assert A.shrink_strength(obs, exp) < 20  # 진짜 차이가 크면 약하게


def test_poisson_rr_finds_station_covariate():
    rng = np.random.default_rng(2)
    x = rng.normal(size=60)
    exp = np.full(60, 200.0)
    obs = rng.poisson(exp * np.exp(0.2 * x))
    r = A.poisson_rr(obs, exp, x)
    assert r["lo"] < np.exp(0.2) < r["hi"] and r["lo"] > 1
    assert A.verdict(r) == "잦음"
    assert A.verdict({"rr": 0.8, "lo": 0.7, "hi": 0.9}) == "드묾"
    assert A.verdict({"rr": 1.0, "lo": 0.9, "hi": 1.1}) == "차이 모름"


def test_holiday_file():
    h = A.load_holidays()
    assert date(2024, 2, 12) in h and date(2025, 10, 6) in h  # 대체공휴일, 추석
    assert all(d.year >= 2022 for d in h)
    assert len(h) > 80


def _fake_panel(seed: int = 3) -> A.Panel:
    """역 6곳 × 소분류 2종 × 2년. 역 0은 1.6배, 주말 1.3배, 소분류 0은 평년보다 습한 날 1.5배, 역 5는 여름에만 1.8배."""
    rng = np.random.default_rng(seed)
    days = [date(2023, 1, 1) + timedelta(days=i) for i in range(730)]
    S, C, D = 6, 2, len(days)
    E = np.full((S, C, D), 0.08, dtype=np.float32)
    wd = np.array([d.weekday() for d in days])
    mon = np.array([d.month for d in days])
    humid_anom = rng.normal(size=(S, D))
    eff = np.ones((S, C, D))
    eff[0] *= 1.6
    eff *= np.where(wd >= 5, 1.3, 1.0)[None, None, :]
    eff[:, 0, :] *= np.where(humid_anom > 0, 1.5, 1.0)
    eff[5] *= np.where(np.isin(mon, (6, 7, 8)), 1.8, 1.0)[None, :]
    O = rng.poisson(E * eff).astype(np.float32)
    weather = {b: np.ones((S, D)) for b in BUNDLES}
    weather.update({b + "_anom": rng.normal(size=(S, D)) for b in BUNDLES})
    weather["humid_anom"] = humid_anom
    cond = {k: rng.random((S, D)) < 0.1 for k in A.CONDITIONS}
    return A.Panel(days[0], days, list(range(1, S + 1)), {i: f"역{i}" for i in range(1, S + 1)}, ["MOTOR", "DCU"], O, E,
                   np.full((S, C), 32), {}, {}, weather=weather, cond=cond, holidays={})


def test_fake_panel_station_ranking_and_when():
    p = _fake_panel()
    cm = np.ones(2, dtype=bool)
    dm = np.ones(len(p.days), dtype=bool)
    rank = A.station_ranking(p, cm, dm)
    assert rank["stations"][0]["station_name"] == "역1" and rank["stations"][0]["verdict"] == "잦음"
    w = A.when(p, cm, dm)
    weekend = w["calendar"][0]
    assert weekend["lo"] < 1.3 < weekend["hi"] and weekend["verdict"] == "잦음"
    assert any(s["station_name"] == "역6" and s["season"] == "여름" for s in w["station_season"])
    # 습기는 소분류 0(MOTOR)만
    only0 = A.when(p, np.array([True, False]), dm)
    only1 = A.when(p, np.array([False, True]), dm)
    hum0 = next(x for x in only0["daily_weather"] if x["bundle"] == "humid")["same_day"]
    hum1 = next(x for x in only1["daily_weather"] if x["bundle"] == "humid")["same_day"]
    assert hum0["verdict"] == "잦음" and 1.25 < hum0["rr"] < 1.8  # 심은 값 1.5(80% 범위는 20%쯤 비껴갈 수 있다)
    assert hum1["verdict"] != "잦음"


def test_day_mask_filters():
    p = _fake_panel()
    assert A.day_mask(p, months=[7]).sum() == 62  # 2년 × 31일
    assert A.day_mask(p, season="겨울").sum() == (31 + 28 + 31) + (31 + 29 + 30)  # 2023-01·02·12, 2024-01·02·12(12/30까지)
    m = A.day_mask(p, daytype="weekend")
    assert all(p.days[i].weekday() >= 5 for i in np.flatnonzero(m))


def test_clean_makes_json_safe():
    out = A.clean({"a": np.float32(1.5), "b": [np.int64(2), float("nan")], 3: np.bool_(True)})
    assert out == {"a": 1.5, "b": [2, None], "3": True}
