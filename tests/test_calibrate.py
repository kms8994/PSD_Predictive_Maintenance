"""psd/calibrate.py 검증: 기록으로 주기를 보정하는 규칙(DB 없이 순수 계산만)."""
import random
from datetime import date

import pytest

from psd.calibrate import calibrate, fit_group, part_intervals

TODAY = date(2026, 10, 31)
START = date(2022, 1, 1)


def test_no_records_keeps_formula():
    c = calibrate(6.5, [])
    assert c.years == pytest.approx(6.5)
    assert c.lo_years < 6.5 < c.hi_years  # 기록이 없으면 범위가 넓다


def test_early_failures_shorten_cycle():
    lives = [(3.0, True)] * 20
    c = calibrate(6.5, lives)
    assert c.years < 6.5
    assert c.failures == 20


def test_long_survival_lengthens_cycle():
    """고장 없이 오래 버틴 부품(중도절단)만 있으면 주기가 길어진다 — 고장 난 것만 세면 짧게 치우친다."""
    lives = [(8.0, False)] * 30
    assert calibrate(6.5, lives).years > 6.5


def test_more_failures_move_further_from_formula_and_narrow_the_range():
    few = calibrate(6.5, [(3.0, True)] * 3)
    many = calibrate(6.5, [(3.0, True)] * 30)
    assert many.years < few.years < 6.5
    assert few.data_share == pytest.approx(3 / (3 + few.prior_weight))
    assert many.hi_years - many.lo_years < few.hi_years - few.lo_years


def test_cycle_is_capped_at_manufacturer_10_years():
    """고장 없이 오래 버틴 기록이 쌓여 계산이 10년을 넘어도 예정일엔 10년까지만 쓴다(계산값은 raw_years에 남긴다)."""
    c = calibrate(9.5, [(9.0, False)] * 40)
    assert c.raw_years > 10
    assert c.years == 10.0 and c.capped
    assert not calibrate(6.5, []).capped


def _weibull_lives(rng, scale, shape, n, horizon):
    out = []
    for _ in range(n):
        t = rng.weibullvariate(scale, shape)
        out.append((min(t, horizon), t <= horizon))
    return out


def test_shape_is_estimated_from_data():
    """같은 평균이라도 마모형(β=3)과 우연 고장형(β=1.2)을 구별한다 — v1은 2로 고정했었다."""
    rng = random.Random(0)
    wear = fit_group([8.0] * 10, [_weibull_lives(rng, 9.0, 3.0, 60, 5.0) for _ in range(10)])
    random_ = fit_group([8.0] * 10, [_weibull_lives(rng, 9.0, 1.2, 60, 5.0) for _ in range(10)])
    assert wear[0].shape > 2.4 and random_[0].shape < 1.6
    assert wear[0].shape_lo < wear[0].shape < wear[0].shape_hi


def test_recovers_true_mean_when_formula_is_wrong():
    """공식이 정답보다 30% 길게 잡았어도 기록이 충분하면 정답 쪽으로 옮겨 간다."""
    rng = random.Random(1)
    true_scale, shape = 6.0, 2.5
    true_mean = true_scale * 0.887264  # Γ(1.4)
    fits = fit_group([true_mean * 1.3] * 20, [_weibull_lives(rng, true_scale, shape, 32, 4.8) for _ in range(20)])
    mean_est = sum(f.raw_years for f in fits) / len(fits)
    assert abs(mean_est / true_mean - 1) < 0.12


# ---------- part_intervals: 조치에 따른 가상 나이 ----------

def test_intervals_failure_and_prevention():
    records = [(date(2024, 1, 1), "fail"), (date(2025, 1, 1), "prev")]
    ints = part_intervals(START, records, TODAY)
    assert [f for _v, _x, f in ints] == [True, False, False]  # 고장, 예방(멀쩡했음), 지금 쓰는 중
    assert ints[0][1] == pytest.approx(2.0, abs=0.01)
    assert ints[1] == (0.0, pytest.approx(1.0, abs=0.01), False)  # 조치 없는 예전 기록 = 전체 교체 → 나이 0


def test_intervals_ignore_records_before_install():
    assert part_intervals(START, [(date(2021, 6, 1), "fail")], TODAY) == [(0.0, (TODAY - START).days / 365.25, False)]


def test_repair_keeps_age_and_partial_halves_it():
    """일부 수리 = 고장 직전과 같음(나이 그대로), 일부 교체 = 나이 절반(가정), 전체 교체 = 새것."""
    ints = part_intervals(START, [(date(2023, 1, 1), "fail", "repair"), (date(2024, 1, 1), "fail", "partial"),
                                  (date(2025, 1, 1), "fail", "full")], date(2026, 1, 1))
    ages = [round(v, 2) for v, _x, _f in ints]
    assert ages == [0.0, 1.0, round((1.0 + 1.0) * 0.5, 2), 0.0]
    assert [f for _v, _x, f in ints] == [True, True, True, False]  # 수리로 끝난 고장도 모두 센다


# ---------- 날씨 민감도 (2026-11-27) ----------


def test_part_segments_follow_records_and_age_factor():
    from psd.calibrate import AGE_FACTOR, part_segments

    segs = part_segments(0, [(100, "fail", "repair"), (300, "fail", "full"), (50, "prev", "partial")], 400)
    assert [(a, b, f) for a, b, f, _ in segs] == [(0, 50, False), (50, 100, True), (100, 300, True), (300, 400, False)]
    assert [x[3] for x in segs[:3]] == [AGE_FACTOR["partial"], AGE_FACTOR["repair"], AGE_FACTOR["full"]]


def test_effective_time_grows_with_weather_and_carries_virtual_age():
    import numpy as np

    from psd.calibrate import _Segs

    sg = _Segs.build([(0, [[(0, 365, True, 1.0), (365, 730, False, 0.0)]])])
    cum = {"humid": np.concatenate([[0.0], np.cumsum(np.full(730, 1.0)) / 365.25])[None, :]}  # 매일 점수 2(점수 − 1 = 1)
    v0, x0, xc = sg.ages(cum, {})
    v1, x1, _ = sg.ages(cum, {"humid": 0.5})
    assert np.allclose(x0, xc) and np.allclose(x1, xc * 1.5)  # 날마다 1.5배로 나이를 먹는다
    assert v1[1] == pytest.approx(x1[0])  # 수리(1.0)라 나이를 그대로 넘긴다


def test_estimate_weather_finds_planted_sensitivity_and_respects_off():
    import numpy as np

    from psd.calibrate import WEATHER_BUNDLES, estimate_weather

    rng = np.random.default_rng(0)
    S, D = 20, 1400
    stress = {b: np.clip(1 + rng.normal(0, 0.8, (S, D)), 0, None) for b in WEATHER_BUNDLES}
    anom = {b: stress[b] - 1 for b in WEATHER_BUNDLES}
    rate = 0.03 * (1 + 0.5 * (stress["humid"] - 1))  # 습기만 민감도 0.5
    fails = {s: list(np.flatnonzero(rng.poisson(rate[s]) > 0)) for s in range(S)}
    ym = np.arange(D) // 30
    info = estimate_weather(fails, np.full(S, 1.0), stress, anom, ym, D, set())
    assert info["w"]["humid"] >= 0.35 and info["p"]["humid"] > 0.9
    assert max(info["w"][b] for b in ("heat", "cold", "wind")) < 0.2  # 무관한 묶음은 0 쪽
    off = estimate_weather(fails, np.full(S, 1.0), stress, anom, ym, D, {"humid"})
    assert off["w"]["humid"] == 0.0 and off["off"] == ["humid"]
