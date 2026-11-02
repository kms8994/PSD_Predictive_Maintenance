"""엑셀·PDF 보고서가 같이 쓰는 계산 (2026-11-02, 보고서를 "무엇을·어디서·몇 개·언제까지" 중심으로 다시 짬).

보고서를 받는 사람(역 PSD 관리자·자재 담당)이 알고 싶은 건 네 가지다.
- 어느 안전문의 어떤 부품을 바꿔야 하나 → work_groups(): 부품·예정일·상태가 같은 문을 한 줄로 묶고 문 번호를 나열
- 얼마나 주문해야 하나 → order_counts(): 부품마다 6개월 안 / 6개월~1년 / 1~2년 / 확인 필요 개수
- 지금 상태와 다음 교체까지 남은 기간 → left_text(), 상태 이름(STATUS_LABEL)
- 돌발 고장 대비 예비품 → 지난 1년 고장 교체 수(부르는 쪽이 DB에서 세어 넘긴다)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from psd.catalog import PART_TYPES, SIDES
from psd.status import RANK, UnitStatus

NAME = {p["code"]: p["name"] for p in PART_TYPES}
PART_ORDER = [p["code"] for p in PART_TYPES]
STATUS_LABEL = {"빨강": "교체 임박", "확인_필요": "확인 필요", "노랑": "주의", "초록": "안전"}

# 주문 수량을 세는 기간. 교체 임박(6개월 이하, 이미 지난 것 포함)·주의(1년 이하)와 같은 경계를 쓴다
WINDOWS = [("6m", "6개월 안", 182), ("1y", "6개월~1년", 365), ("2y", "1~2년", 730)]


@dataclass(frozen=True)
class DoorPos:
    side: str  # "U" | "D"
    car: int
    position: int

    @property
    def no(self) -> str:
        return f"{self.car}-{self.position}"


@dataclass
class WorkGroup:
    """교체 작업 한 줄: 한 역에서 같은 부품·같은 예정일·같은 상태인 부품들."""

    station_id: int
    part_type_code: str
    status: str
    due: date
    days_left: int
    doors: list[DoorPos] = field(default_factory=list)  # 역 공통 부품(종합제어반)은 비어 있다
    count: int = 0


def left_text(days: int) -> str:
    """남은 기간을 "2개월 남음" / "40일 지남"처럼. 개월은 버림(화면의 dur()와 같다 — 182일이 "6개월"로 안 보이게)."""
    if days < 0:
        return f"{-days}일 지남"
    m = int(days / 30.44)
    t = (f"{m // 12}년" + (f" {m % 12}개월" if m % 12 else "")) if m >= 12 else f"{m}개월" if m else f"{days}일"
    return t + " 남음"


def window_of(u: UnitStatus) -> str | None:
    """주문 수량 기간. 확인 필요는 언제 바꿀지 몰라 따로 센다(None이 아니라 "check")."""
    if u.status == "확인_필요":
        return "check"
    for key, _label, days in WINDOWS:
        if u.days_left <= days:
            return key
    return None


def work_groups(statuses: list[UnitStatus], doors: dict[int, DoorPos], horizon_days: int = 365) -> list[WorkGroup]:
    """1년 안 교체할 부품(+ 확인 필요)을 역 x 부품 x 예정일 x 상태로 묶는다. 급한 순."""
    groups: dict[tuple, WorkGroup] = {}
    for u in statuses:
        if u.status == "초록" and u.days_left > horizon_days:
            continue
        key = (u.station_id, u.part_type_code, u.due, u.status)
        g = groups.get(key)
        if g is None:
            g = groups[key] = WorkGroup(u.station_id, u.part_type_code, u.status, u.due, u.days_left)
        g.count += 1
        if u.door_id is not None:
            g.doors.append(doors[u.door_id])
    for g in groups.values():
        g.doors.sort(key=lambda d: (d.side != "U", d.car, d.position))
    return sorted(groups.values(), key=lambda g: (RANK[g.status], g.days_left, g.station_id, PART_ORDER.index(g.part_type_code)))


def doors_text(g: WorkGroup, sep: str = " / ") -> str:
    """대상 안전문: "부전 방면 1-4, 2-1 / 북울산 방면 1-1". 역 공통 부품은 "종합제어반"."""
    if not g.doors:
        return "종합제어반(역 공통)"
    parts = []
    for side, label in SIDES.items():
        nos = [d.no for d in g.doors if d.side == side]
        if nos:
            parts.append(f"{label} {', '.join(nos)}")
    return sep.join(parts)


def order_counts(statuses: list[UnitStatus]) -> dict[str, dict[str, int]]:
    """부품마다 기간별 교체 개수 {코드: {"6m": n, "1y": n, "2y": n, "check": n}}. 부품 순서는 PART_TYPES."""
    out = {code: {"6m": 0, "1y": 0, "2y": 0, "check": 0} for code in PART_ORDER}
    present = set()
    for u in statuses:
        present.add(u.part_type_code)
        w = window_of(u)
        if w:
            out[u.part_type_code][w] += 1
    return {code: v for code, v in out.items() if code in present}
