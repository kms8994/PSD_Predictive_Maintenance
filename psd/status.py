"""문·부품의 현재 경고 상태 (설계_v2.md 6-3, 목업의 unitState와 같은 규칙).

빨강(6개월 이하, 이미 지난 경우 포함) > 확인 필요(기록 없이 예정일이 지남) > 노랑(1년 이하) > 초록.
"확인 필요"가 날짜만으로 못 가리는 경우를 가려내는 자리라 "빨강"보다는 덜 급한 걸로 둔다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from psd.models import CycleResult, Part

RANK = {"빨강": 0, "확인_필요": 1, "노랑": 2, "초록": 3}


@dataclass(frozen=True)
class UnitStatus:
    part_id: int
    station_id: int
    door_id: int | None
    part_type_code: str
    install_date: date
    has_record: bool
    cycle: CycleResult
    due: date
    days_left: int
    status: str


def next_due_date(install_date: date, base_years: float) -> date:
    return install_date + timedelta(days=round(base_years * 365.25))


def judge_status(days_left: int, has_record: bool) -> str:
    if days_left < 0 and not has_record:
        return "확인_필요"
    if days_left <= 182:
        return "빨강"
    if days_left <= 365:
        return "노랑"
    return "초록"


def to_unit_status(part: Part, cycle: CycleResult, today: date | None = None) -> UnitStatus:
    today = today or date.today()
    due = next_due_date(part.install_date, cycle.base_years)
    days_left = (due - today).days
    return UnitStatus(
        part_id=part.id,
        station_id=part.station_id,
        door_id=part.door_id,
        part_type_code=part.part_type_code,
        install_date=part.install_date,
        has_record=part.has_record,
        cycle=cycle,
        due=due,
        days_left=days_left,
        status=judge_status(days_left, part.has_record),
    )


def _joined_parts(session: Session, station_id: int | None = None):
    stmt = select(Part, CycleResult).join(
        CycleResult,
        (CycleResult.station_id == Part.station_id) & (CycleResult.part_type_code == Part.part_type_code),
    )
    if station_id is not None:
        stmt = stmt.where(Part.station_id == station_id)
    return session.execute(stmt).all()


def station_unit_statuses(session: Session, station_id: int, today: date | None = None) -> list[UnitStatus]:
    return [to_unit_status(part, cycle, today) for part, cycle in _joined_parts(session, station_id)]


def all_unit_statuses(session: Session, today: date | None = None) -> list[UnitStatus]:
    return [to_unit_status(part, cycle, today) for part, cycle in _joined_parts(session)]


def worst(statuses: list[UnitStatus]) -> UnitStatus:
    return min(statuses, key=lambda u: (RANK[u.status], u.days_left))


def counts(statuses: list[UnitStatus]) -> dict[str, int]:
    out = {"빨강": 0, "노랑": 0, "확인_필요": 0, "초록": 0}
    for u in statuses:
        out[u.status] += 1
    return out
