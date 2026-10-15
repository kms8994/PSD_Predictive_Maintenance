"""psd/status.py 검증: 목업의 unitState와 같은 규칙인지 (MVP_계획.md 4단계).

빨강(6개월 이하, 지난 경우 포함) > 확인 필요(기록 없이 예정일 지남) > 노랑(1년 이하) > 초록.
"""
from datetime import date, timedelta

from psd.models import CycleResult, Part
from psd.status import counts, judge_status, next_due_date, to_unit_status, worst

TODAY = date(2026, 10, 15)


def _part(install_date: date, has_record: bool, **kw) -> Part:
    return Part(id=kw.get("id", 1), part_type_code=kw.get("part_type_code", "DCM"), station_id=1, door_id=1, install_date=install_date, has_record=has_record)


def _cycle(base_years: float, judgment: str = "같음") -> CycleResult:
    return CycleResult(station_id=1, part_type_code="DCM", base_years=base_years, lo_years=base_years, hi_years=base_years, judgment=judgment)


def test_overdue_without_record_is_check():
    part = _part(install_date=TODAY - timedelta(days=4000), has_record=False)
    unit = to_unit_status(part, _cycle(base_years=5.0), today=TODAY)
    assert unit.days_left < 0
    assert unit.status == "확인_필요"


def test_overdue_with_record_is_red_not_check():
    """기록이 있으면(설치일을 신뢰할 수 있으면) 지났어도 "확인 필요"가 아니라 "빨강"이다."""
    part = _part(install_date=TODAY - timedelta(days=4000), has_record=True)
    unit = to_unit_status(part, _cycle(base_years=5.0), today=TODAY)
    assert unit.days_left < 0
    assert unit.status == "빨강"


def test_due_within_six_months_is_red():
    part = _part(install_date=TODAY, has_record=True)
    unit = to_unit_status(part, _cycle(base_years=0.49), today=TODAY)  # 약 179일
    assert 0 <= unit.days_left <= 182
    assert unit.status == "빨강"


def test_due_within_a_year_is_amber():
    part = _part(install_date=TODAY, has_record=True)
    unit = to_unit_status(part, _cycle(base_years=0.9), today=TODAY)  # 약 329일
    assert 182 < unit.days_left <= 365
    assert unit.status == "노랑"


def test_due_far_away_is_green():
    part = _part(install_date=TODAY, has_record=True)
    unit = to_unit_status(part, _cycle(base_years=10.0), today=TODAY)
    assert unit.days_left > 365
    assert unit.status == "초록"


def test_next_due_date_uses_365_25_days_per_year():
    install = date(2016, 12, 30)
    due = next_due_date(install, 10.0)
    assert due == install + timedelta(days=round(10.0 * 365.25))


def test_judge_status_matches_rules_directly():
    assert judge_status(days_left=-10, has_record=False) == "확인_필요"
    assert judge_status(days_left=-10, has_record=True) == "빨강"
    assert judge_status(days_left=182, has_record=True) == "빨강"
    assert judge_status(days_left=183, has_record=True) == "노랑"
    assert judge_status(days_left=365, has_record=True) == "노랑"
    assert judge_status(days_left=366, has_record=True) == "초록"


def test_worst_ranks_red_above_check_above_amber_above_green():
    """목업의 RANK = {red:0, check:1, amber:2, green:3}과 같은 순서인지."""
    red = to_unit_status(_part(TODAY, True), _cycle(0.1), today=TODAY)
    check = to_unit_status(_part(TODAY - timedelta(days=4000), False), _cycle(5.0), today=TODAY)
    amber = to_unit_status(_part(TODAY, True), _cycle(0.9), today=TODAY)
    green = to_unit_status(_part(TODAY, True), _cycle(10.0), today=TODAY)

    assert worst([check, amber, green, red]) is red
    assert worst([check, amber, green]) is check
    assert worst([amber, green]) is amber
    assert worst([green]) is green


def test_worst_breaks_ties_by_days_left():
    sooner = to_unit_status(_part(TODAY, True), _cycle(0.1), today=TODAY)
    later = to_unit_status(_part(TODAY, True, id=2), _cycle(0.49), today=TODAY)
    assert sooner.days_left < later.days_left
    assert worst([later, sooner]) is sooner


def test_counts_tally_all_four_statuses():
    units = [
        to_unit_status(_part(TODAY, True), _cycle(0.1), today=TODAY),  # 빨강
        to_unit_status(_part(TODAY - timedelta(days=4000), False), _cycle(5.0), today=TODAY),  # 확인_필요
        to_unit_status(_part(TODAY, True), _cycle(0.9), today=TODAY),  # 노랑
        to_unit_status(_part(TODAY, True), _cycle(10.0), today=TODAY),  # 초록
    ]
    assert counts(units) == {"빨강": 1, "노랑": 1, "확인_필요": 1, "초록": 1}
