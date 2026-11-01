"""psd/calibrate.py 검증: 고장 기록으로 주기를 보정하는 규칙(DB 없이 순수 계산만)."""
from datetime import date

import pytest

from psd.calibrate import PRIOR_FAILURES, calibrate, part_lives

TODAY = date(2026, 10, 31)
START = date(2022, 1, 1)


def test_no_records_keeps_formula():
    assert calibrate(6.5, []).years == pytest.approx(6.5)


def test_early_failures_shorten_cycle():
    lives = [(3.0, True)] * 20
    c = calibrate(6.5, lives)
    assert c.years < 6.5
    assert c.failures == 20


def test_long_survival_lengthens_cycle():
    """고장 없이 오래 버틴 부품(중도절단)만 있으면 주기가 길어진다 — 고장 난 것만 세면 짧게 치우친다."""
    lives = [(8.0, False)] * 30
    assert calibrate(6.5, lives).years > 6.5


def test_more_failures_move_further_from_formula():
    few = calibrate(6.5, [(3.0, True)] * 3)
    many = calibrate(6.5, [(3.0, True)] * 30)
    assert many.years < few.years < 6.5
    assert few.data_share == pytest.approx(3 / (3 + PRIOR_FAILURES))


def test_part_lives_counts_only_failures_as_end_of_life():
    records = [(date(2024, 1, 1), "fail"), (date(2025, 1, 1), "prev")]
    lives = part_lives(START, records, TODAY)
    assert [failed for _, failed in lives] == [True, False, False]  # 고장, 예방 교체(멀쩡했음), 지금 쓰는 중
    assert lives[0][0] == pytest.approx(2.0, abs=0.01)
    assert lives[1][0] == pytest.approx(1.0, abs=0.01)


def test_part_lives_ignores_records_before_install():
    lives = part_lives(START, [(date(2021, 6, 1), "fail")], TODAY)
    assert lives == [((TODAY - START).days / 365.25, False)]


def test_part_lives_inspection_does_not_end_life():
    """점검(이상 없음)은 부품을 바꾼 게 아니라 수명을 끊지 않는다."""
    with_check = part_lives(START, [(date(2024, 1, 1), "fail"), (date(2025, 6, 1), "check")], TODAY)
    without = part_lives(START, [(date(2024, 1, 1), "fail")], TODAY)
    assert with_check == without


def test_cycle_is_capped_at_manufacturer_10_years():
    """고장 없이 오래 버틴 기록이 쌓여 계산이 10년을 넘어도 예정일엔 10년까지만 쓴다(계산값은 raw_years에 남긴다)."""
    c = calibrate(9.5, [(9.0, False)] * 40)
    assert c.raw_years > 10
    assert c.years == 10.0 and c.capped
    assert not calibrate(6.5, []).capped
