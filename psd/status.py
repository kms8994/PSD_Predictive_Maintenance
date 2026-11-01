"""문·부품의 현재 경고 상태 (설계_v2.md 6-3, 목업의 unitState와 같은 규칙).

빨강(6개월 이하, 이미 지난 경우 포함) > 확인 필요(기록 없이 예정일이 지남) > 노랑(1년 이하) > 초록.
"확인 필요"가 날짜만으로 못 가리는 경우를 가려내는 자리라 "빨강"보다는 덜 급한 걸로 둔다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from psd.calibrate import Calibrated, calibration_map, data_version
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
    cal: Calibrated | None = None  # 기록 반영 주기(psd/calibrate.py). 없으면 날씨 공식 주기 그대로

    @property
    def cycle_years(self) -> float:
        """예정일 계산에 쓴 주기: 기록 반영 주기가 있으면 그것, 없으면 날씨 공식 주기."""
        return self.cal.years if self.cal else self.cycle.base_years


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


def to_unit_status(part: Part, cycle: CycleResult, today: date | None = None, cal: Calibrated | None = None) -> UnitStatus:
    today = today or date.today()
    due = next_due_date(part.install_date, cal.years if cal else cycle.base_years)
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
        cal=cal,
    )


@dataclass(frozen=True)
class CycleView:
    """계산 결과(cycle_results) 한 행의 값 복사본. 캐시에 ORM 객체를 두면 그 세션이 닫히거나 커밋된 뒤 읽을 수 없다."""

    station_id: int
    part_type_code: str
    base_years: float
    lo_years: float
    hi_years: float
    judgment: str


_rows_cache: dict = {}  # {"key": 데이터 버전, "rows": [(부품 행, CycleView), ...]}


def _joined_parts(session: Session, station_id: int | None = None):
    """부품(열 값만) + 그 역 x 부품 종류의 계산 결과.

    부품 4,400개를 ORM 객체로 만들면 0.6초, 열만 읽어도 MySQL에서 받아오는 데 0.2초가 걸려 현황 화면
    (API 세 개를 동시에 부름)이 3~4초씩 걸렸다. 부품 설치일은 교체 기록을 넣거나 지울 때만 바뀌므로
    psd/calibrate.py와 같은 데이터 버전으로 묶어 두고, 바뀌었을 때만 다시 읽는다.
    """
    key = data_version(session, date.today())
    if _rows_cache.get("key") != key:
        cycles = {
            (c.station_id, c.part_type_code): CycleView(c.station_id, c.part_type_code, c.base_years, c.lo_years, c.hi_years, c.judgment)
            for c in session.execute(select(CycleResult)).scalars()
        }
        part_q = select(Part.id, Part.station_id, Part.door_id, Part.part_type_code, Part.install_date, Part.has_record)
        rows = [(row, cycles[(row.station_id, row.part_type_code)]) for row in session.execute(part_q)
                if (row.station_id, row.part_type_code) in cycles]
        _rows_cache.update(key=key, rows=rows)
    rows = _rows_cache["rows"]
    return rows if station_id is None else [r for r in rows if r[0].station_id == station_id]


def station_unit_statuses(session: Session, station_id: int, today: date | None = None) -> list[UnitStatus]:
    cal = calibration_map(session, today, station_id)
    return [to_unit_status(part, cycle, today, cal.get((part.station_id, part.part_type_code)))
            for part, cycle in _joined_parts(session, station_id)]


def all_unit_statuses(session: Session, today: date | None = None) -> list[UnitStatus]:
    cal = calibration_map(session, today)
    return [to_unit_status(part, cycle, today, cal.get((part.station_id, part.part_type_code)))
            for part, cycle in _joined_parts(session)]


def worst(statuses: list[UnitStatus]) -> UnitStatus:
    return min(statuses, key=lambda u: (RANK[u.status], u.days_left))


def counts(statuses: list[UnitStatus]) -> dict[str, int]:
    out = {"빨강": 0, "노랑": 0, "확인_필요": 0, "초록": 0}
    for u in statuses:
        out[u.status] += 1
    return out
