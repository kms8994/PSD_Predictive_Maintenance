"""psd/demand.py 검증: 내년 부품 사용량 시뮬레이션 (2026-11-28). DB 없이 돈다 — 답을 손으로 알 수 있는 경우만 쓴다."""
import math

import numpy as np

from psd.demand import simulate, summarize

ACTIONS = ["full", "partial", "repair"]
FACTOR = np.array([0.0, 0.5, 1.0])


def _run(n, age=0.0, eta=2.0, beta=1.0, mix=(1.0, 0.0, 0.0), group=None, t0=0.0, t1=1.0, sigma=0.0, n_sims=200, seed=1):
    group = np.zeros(n, dtype=int) if group is None else np.asarray(group)
    return simulate(np.full(n, age, dtype=float), np.full(n, eta), np.full(n, beta), np.full(n, sigma), np.zeros(n, dtype=int),
                    group, int(group.max()) + 1, np.tile(mix, (n, 1)), FACTOR, t0, t1, n_sims=n_sims, seed=seed)


def test_exponential_life_gives_horizon_over_mean():
    """β = 1(나이와 무관), 평균 2년, 1년 → 부품당 평균 0.5건."""
    res = _run(1000)
    per_unit = res.counts[:, 1].sum(axis=(1, 2)).mean() / 1000
    assert math.isclose(per_unit, 0.5, rel_tol=0.03)
    assert res.counts[:, 0].sum() == 0  # 올해 남은 기간이 0이면 거기엔 없다


def test_older_parts_fail_more_when_wear_out():
    """β > 1이면 나이가 많은 부품이 내년에 더 많이 고장 난다(지금 나이에서 이어 굴린다)."""
    young, old = _run(500, age=0.0, eta=5.0, beta=3.0), _run(500, age=5.0, eta=5.0, beta=3.0)
    assert old.counts[:, 1].sum() > 3 * young.counts[:, 1].sum()


def test_repair_keeps_age_so_more_failures_than_replacement():
    """수리는 나이를 그대로 두고(다시 빨리 고장), 교체는 새것으로 만든다."""
    kw = dict(age=4.0, eta=5.0, beta=3.0, t1=3.0)
    assert _run(500, mix=(0.0, 0.0, 1.0), **kw).counts.sum() > 1.3 * _run(500, mix=(1.0, 0.0, 0.0), **kw).counts.sum()


def test_actions_follow_the_mix_and_windows_split_this_year_and_next():
    res = _run(2000, mix=(0.7, 0.3, 0.0), t0=0.5, t1=1.5)
    nxt = res.counts[:, 1, 0].sum(axis=0)  # 조치별
    assert math.isclose(nxt[0] / nxt.sum(), 0.7, abs_tol=0.02) and nxt[2] == 0
    rest = res.counts[:, 0].sum()
    assert math.isclose(rest / res.counts[:, 1].sum(), 0.5, rel_tol=0.08)  # 반년 vs 1년(지수 수명)
    assert np.allclose(res.unit_mean.sum(axis=0), res.counts.mean(axis=0).sum(axis=1))  # 부품 평균의 합 = 묶음 합의 평균


def test_groups_add_up_to_the_whole():
    g = np.array([0, 1] * 300)
    res = _run(600, group=g)
    whole = _run(600)
    assert res.counts.sum() == whole.counts.sum()  # 같은 시드·같은 부품이면 묶음만 나뉜다
    assert res.counts[:, 1, 0].sum() > 0 and res.counts[:, 1, 1].sum() > 0


def test_life_uncertainty_widens_the_range_and_lifts_the_mean_by_jensen():
    """수명을 가운데 값 둘레로 흔들면 범위가 넓어지고, 지수 수명에선 평균 건수가 정확히 e^(σ²/2)배가 된다(건수 ∝ 1/수명)."""
    narrow = _run(400, sigma=0.0, n_sims=400).counts[:, 1].sum(axis=(1, 2))
    wide = _run(400, sigma=0.4, n_sims=400).counts[:, 1].sum(axis=(1, 2))
    assert wide.std() > 2 * narrow.std()
    assert math.isclose(wide.mean() / narrow.mean(), math.exp(0.4 ** 2 / 2), rel_tol=0.06)


def test_summarize_gives_mean_and_80pct_range():
    s = summarize(np.arange(101))
    assert s == {"mean": 50.0, "lo": 10, "hi": 90}
    assert summarize(np.zeros(5)) == {"mean": 0.0, "lo": 0, "hi": 0}
