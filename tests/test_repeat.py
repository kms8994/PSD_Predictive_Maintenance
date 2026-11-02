"""반복 고장 문 판정(psd/repeat.py)."""
from datetime import date

import pytest

from psd.repeat import REPEAT_MIN_FAILURES, find_repeat_doors, poisson_tail, window_start

TODAY = date(2026, 11, 3)
D = date(2026, 1, 15)  # 비교 기간 안의 아무 날


def test_poisson_tail():
    assert poisson_tail(0, 1.0) == 1.0
    assert poisson_tail(1, 1.0) == pytest.approx(1 - 0.36788, abs=1e-4)
    assert poisson_tail(5, 1.1) == pytest.approx(0.0054, abs=1e-4)
    assert poisson_tail(3, 0.0) == 0.0


def test_window_start_leap_day():
    assert window_start(date(2028, 2, 29)) == date(2025, 2, 28)


def _station(n_doors: int, fails: dict[int, int], st: int = 1, day: date = D):
    doors = {st: list(range(1, n_doors + 1))}
    failures = [(d, day, ["IFM"]) for d, k in fails.items() for _ in range(k)]
    return failures, doors


def test_flags_door_far_above_peers():
    # 32개 문이 1건씩인데 한 문만 5건
    failures, doors = _station(32, {**{d: 1 for d in range(2, 33)}, 1: 5})
    out = find_repeat_doors(failures, doors, TODAY)
    assert out[1].flagged
    assert out[1].peer_mean == pytest.approx(1.0)
    assert out[1].ratio == pytest.approx(5.0)
    assert not any(out[d].flagged for d in range(2, 33))


def test_not_flagged_when_whole_station_fails_often():
    # 역 전체가 4건씩이면 그 문만의 문제가 아니다(날씨 쪽 — 교체주기 보정이 맡음)
    failures, doors = _station(32, {d: 4 for d in range(1, 33)})
    out = find_repeat_doors(failures, doors, TODAY)
    assert not any(r.flagged for r in out.values())


def test_needs_minimum_failures():
    # 다른 문이 모두 0건이어도 2건은 표시하지 않는다(우연히도 흔히 나옴)
    failures, doors = _station(32, {1: REPEAT_MIN_FAILURES - 1})
    assert not find_repeat_doors(failures, doors, TODAY)[1].flagged
    failures, doors = _station(32, {1: REPEAT_MIN_FAILURES})
    assert find_repeat_doors(failures, doors, TODAY)[1].flagged


def test_old_failures_outside_window_are_ignored():
    failures, doors = _station(32, {1: 5}, day=date(2022, 6, 1))
    r = find_repeat_doors(failures, doors, TODAY)[1]
    assert r.failures == 0 and not r.flagged


def test_compares_only_within_station():
    # 1번 역은 문마다 3건, 2번 역은 0건 — 1번 역 문들은 자기 역 평균과 같아서 표시 안 됨
    doors = {1: [1, 2, 3, 4], 2: [5, 6, 7, 8]}
    failures = [(d, D, ["DCM"]) for d in (1, 2, 3, 4) for _ in range(3)]
    out = find_repeat_doors(failures, doors, TODAY)
    assert not any(r.flagged for r in out.values())
    assert out[5].failures == 0 and out[5].peer_mean == 0


def test_by_part_and_last():
    doors = {1: [1, 2]}
    failures = [(1, date(2025, 3, 1), ["IFM"]), (1, date(2026, 5, 1), ["IFM", "DCM"])]
    r = find_repeat_doors(failures, doors, TODAY)[1]
    assert r.failures == 2
    assert r.by_part == {"IFM": 2, "DCM": 1}
    assert r.last == date(2026, 5, 1)


def test_single_door_station_has_no_peers():
    out = find_repeat_doors([(1, D, ["IFM"])] * 5, {1: [1]}, TODAY)
    assert out[1].peer_mean is None and not out[1].flagged
