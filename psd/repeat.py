"""반복 고장 문 찾기 (향후_방향_MVP이후.md 2장 "반복 고장 위험도", 2026-11-03).

"고장 2건 이상"처럼 건수만 보면 더미 기록 기준으로 문 736개 중 387개(53%)가 걸려 쓸모가 없다. 그래서
문마다 최근 REPEAT_WINDOW_YEARS년 고장 건수를 **같은 역 다른 문들의 평균**과 비교하고, 그 평균이라면
우연히 이만큼 나올 확률(포아송 분포)이 REPEAT_P_MAX보다 작고 REPEAT_MIN_FAILURES건 이상일 때만 표시한다.

- 같은 역끼리만 비교한다: 날씨·설치일·운행 횟수가 같아서, 역 전체가 고장이 잦은 것(날씨 영향 — 교체주기
  보정이 맡음)과 그 문만 유난한 것(설치·정렬·배수 같은 문 자체 문제)을 나눌 수 있다.
- 고장 1건 = 고장 사유 기록 1건(부품 여러 개를 한 번에 바꿔도 1건). 예방·함께 교체·점검은 세지 않는다.
- 역 공통 부품(종합제어반)은 문이 아니라 뺀다.
- 문이 736개라 기준을 느슨하게(예: 5%) 두면 고장이 무작위로만 나도 수십 개가 걸린다. 1%에 3건 이상이면
  더미 기록(무작위로 만든 것)에서 2개 안팎만 걸린다 — "점검 우선 후보"로 보여 주는 정도가 맞다.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

REPEAT_WINDOW_YEARS = 3
REPEAT_MIN_FAILURES = 3
REPEAT_P_MAX = 0.01


def poisson_tail(n: int, mu: float) -> float:
    """평균이 mu인 포아송 분포에서 n건 이상 나올 확률 P(X >= n)."""
    if n <= 0:
        return 1.0
    if mu <= 0:
        return 0.0
    term = math.exp(-mu)
    below = term
    for k in range(1, n):
        term *= mu / k
        below += term
    return max(0.0, 1.0 - below)


def window_start(today: date) -> date:
    """비교 기간의 첫날(이날 포함). 2/29는 2/28로."""
    try:
        return today.replace(year=today.year - REPEAT_WINDOW_YEARS)
    except ValueError:
        return today.replace(year=today.year - REPEAT_WINDOW_YEARS, day=28)


@dataclass
class DoorRepeat:
    door_id: int
    station_id: int
    failures: int  # 기간 안 고장 건수
    peer_mean: float | None  # 같은 역 다른 문의 평균 고장 건수(비교할 문이 없으면 None)
    p_value: float | None  # 그 평균이라면 이만큼 이상 나올 확률
    flagged: bool
    by_part: Counter = field(default_factory=Counter)  # 부품 종류 코드 → 고장 건수
    last: date | None = None  # 기간 안 마지막 고장일

    @property
    def ratio(self) -> float | None:
        """다른 문 평균의 몇 배인지. 평균이 0이면 None."""
        return self.failures / self.peer_mean if self.peer_mean else None


def find_repeat_doors(
    failures: list[tuple[int, date, list[str]]],
    doors_by_station: dict[int, list[int]],
    today: date,
) -> dict[int, DoorRepeat]:
    """문마다 반복 고장 통계를 낸다.

    failures: [(문 id, 고장일, [부품 종류 코드, ...]), ...] — 고장 사유 기록만, 문이 있는 것만.
    doors_by_station: 역 id → 그 역 문 id 목록(고장이 0건인 문도 평균에 들어가야 하므로 전부).
    """
    start = window_start(today)
    count: Counter = Counter()
    by_part: dict[int, Counter] = {}
    last: dict[int, date] = {}
    for door_id, day, codes in failures:
        if not (start <= day <= today):
            continue
        count[door_id] += 1
        by_part.setdefault(door_id, Counter()).update(codes)
        if door_id not in last or day > last[door_id]:
            last[door_id] = day

    out: dict[int, DoorRepeat] = {}
    for station_id, ids in doors_by_station.items():
        total = sum(count[d] for d in ids)
        for d in ids:
            n = count[d]
            if len(ids) < 2:
                peer, p = None, None
            else:
                peer = (total - n) / (len(ids) - 1)
                p = poisson_tail(n, peer)
            flagged = p is not None and n >= REPEAT_MIN_FAILURES and p < REPEAT_P_MAX
            out[d] = DoorRepeat(d, station_id, n, peer, p, flagged, by_part.get(d, Counter()), last.get(d))
    return out
