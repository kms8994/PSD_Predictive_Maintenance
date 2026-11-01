"""교체(고장) 기록으로 권장 주기를 보정한다 (향후_방향_MVP이후.md 2장, 2026-10-31 시연 준비).

방법: 날씨 공식이 낸 주기(psd/cycle.py, DB의 cycle_results.base_years)를 사전값으로 두고,
역 x 부품 종류마다 실제 기록을 와이블 생존분석 + 베이지안 갱신으로 반영한다.

- 와이블 형상모수는 2로 고정한다(scripts/generate_dummy_records.py와 같은 가정 — 오래 쓸수록
  고장 위험이 커지는 마모 고장). 형상이 정해져 있으면 t^2가 지수분포를 따르므로, 고장률
  λ = scale^-2 에 감마 사전분포를 두면 계산이 닫힌 식(켤레)으로 끝난다.
- 사전값의 무게는 "고장 PRIOR_FAILURES건만큼"으로 둔다. 기록이 적은 조합은 공식 값을 거의
  그대로 쓰고, 고장이 쌓일수록 기록 쪽으로 옮겨 간다(데이터가 적어 일반 ML 학습은 안 맞음).
- 아직 고장 안 난 부품도 데이터다(중도절단): "4년째 멀쩡함 = 최소 4년은 버팀"으로 넣는다.
  고장 교체만 수명 끝으로 세고, 예방 교체·함께 교체는 그때까지 멀쩡했다는 중도절단으로 센다.

    lives: [(쓴 기간(년), 고장으로 끝났는지), ...]
    posterior: a = a0 + 고장 수,  b = b0 + Σ 쓴 기간^2
    scale = (b / a)^(1/2),  주기(평균 수명) = scale × Γ(1.5)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from psd.cycle import STATIONS
from psd.models import CycleResult, Part, ReplacementPart, ReplacementRecord

WEIBULL_SHAPE = 2.0
PRIOR_FAILURES = 8.0  # 날씨 공식 값을 "고장 8건을 본 것"만큼 믿는다. 4로 두면 더미데이터(역 x 부품당 고장 10~20건)의
# 우연한 차이만으로 주기가 ±25%까지 흔들려서, 역 사이 차이가 날씨보다 우연에 더 좌우됐다(2026-10-31 확인)
_GAMMA = math.gamma(1 + 1 / WEIBULL_SHAPE)

# 역 id → PSD 사용개시일. 부품의 첫 설치일이라 기록 사이 기간을 셀 출발점이 된다(scripts/init_db.py와 같은 값)
PSD_START = {s.order: s.psd_install_date or s.open_date for s in STATIONS}


@dataclass(frozen=True)
class Calibrated:
    formula_years: float  # 날씨 공식이 낸 주기(사전값)
    years: float  # 기록을 반영한 주기 — 예정일 계산에 쓴다
    failures: int  # 반영한 고장 수
    units: int  # 지켜본 부품 수명 수(고장·교체로 끝난 것 + 지금 쓰는 중인 것)
    observed_years: float  # 지켜본 기간 합(년)

    @property
    def data_share(self) -> float:
        """결과에서 기록이 차지하는 몫(0~1). 고장 수 기준의 대략적인 비율이다."""
        return self.failures / (self.failures + PRIOR_FAILURES)


def calibrate(formula_years: float, lives: list[tuple[float, bool]]) -> Calibrated:
    scale0 = formula_years / _GAMMA
    a = PRIOR_FAILURES
    b = PRIOR_FAILURES * scale0 ** WEIBULL_SHAPE
    failures = 0
    observed = 0.0
    for years, failed in lives:
        if years <= 0:
            continue
        b += years ** WEIBULL_SHAPE
        observed += years
        if failed:
            a += 1
            failures += 1
    years = (b / a) ** (1 / WEIBULL_SHAPE) * _GAMMA
    return Calibrated(formula_years, years, failures, sum(1 for y, _ in lives if y > 0), observed)


def part_lives(start: date, records: list[tuple[date, str]], today: date) -> list[tuple[float, bool]]:
    """부품 하나의 수명 목록. records는 (교체일, 사유 코드). 마지막은 지금 쓰는 중인 부품(중도절단).

    점검(이상 없음)은 "그날까지 멀쩡했다"는 기록이라 수명을 끊지 않는다. 지금 쓰는 부품은 이미 오늘까지
    버틴 것으로 세고 있어 계산 값은 바뀌지 않는다 — 점검 기록은 그 가정(기록 없음 = 고장 없음)의 근거가 된다.
    """
    lives = []
    for replaced_on, reason in sorted(records):
        if reason == "check":
            continue
        if replaced_on <= start:
            continue  # 사용개시 전 날짜나 같은 날 두 번 넣은 기록은 수명 계산에서 뺀다
        lives.append(((replaced_on - start).days / 365.25, reason == "fail"))
        start = replaced_on
    if today > start:
        lives.append(((today - start).days / 365.25, False))
    return lives


_cache: dict = {}  # {"key": 데이터 버전, "map": 전체 역 결과} — 마지막 한 벌만 둔다


def data_version(session: Session, today: date) -> tuple:
    """기록·계산 결과·부품이 바뀌었는지 가늠하는 값. 기록을 넣거나 지우면 개수나 최대 id가 바뀐다."""
    rec = session.execute(select(func.count(ReplacementRecord.id), func.max(ReplacementRecord.id))).one()
    rp = session.execute(select(func.count(ReplacementPart.id), func.max(ReplacementPart.id))).one()
    cr = session.execute(select(func.count(), func.sum(CycleResult.base_years)).select_from(CycleResult)).one()
    parts = session.scalar(select(func.count(Part.id)))
    return (today, tuple(rec), tuple(rp), tuple(cr), parts)


def calibration_map(session: Session, today: date | None = None, station_id: int | None = None,
                    use_cache: bool = True) -> dict[tuple[int, str], Calibrated]:
    """역 x 부품 종류마다 기록을 반영한 주기.

    현황 화면은 API 세 개가 동시에 이걸 부르므로, 기록이 그대로면 마지막 결과를 다시 쓴다(약 0.3초 → 수 ms).
    업로드 전후 비교처럼 아직 커밋 안 된 상태를 봐야 할 때는 use_cache=False로 부른다.
    """
    today = today or date.today()
    if use_cache:
        key = data_version(session, today)
        if _cache.get("key") != key:
            _cache.update(key=key, map=_compute(session, today, None))
        full = _cache["map"]
        return full if station_id is None else {k: v for k, v in full.items() if k[0] == station_id}
    return _compute(session, today, station_id)


def _compute(session: Session, today: date, station_id: int | None) -> dict[tuple[int, str], Calibrated]:
    cr_q = select(CycleResult)
    rec_q = (select(ReplacementRecord.station_id, ReplacementRecord.door_id, ReplacementPart.part_type_code,
                    ReplacementRecord.replaced_on, ReplacementRecord.reason)
             .join(ReplacementPart, ReplacementPart.record_id == ReplacementRecord.id))
    part_q = select(Part.station_id, Part.door_id, Part.part_type_code)
    if station_id is not None:
        cr_q = cr_q.where(CycleResult.station_id == station_id)
        rec_q = rec_q.where(ReplacementRecord.station_id == station_id)
        part_q = part_q.where(Part.station_id == station_id)
    formula = {(cr.station_id, cr.part_type_code): cr.base_years for cr in session.execute(cr_q).scalars()}

    records: dict[tuple[int, int | None, str], list[tuple[date, str]]] = {}
    for sid, door_id, code, replaced_on, reason in session.execute(rec_q):
        records.setdefault((sid, door_id, code), []).append((replaced_on, reason))

    lives: dict[tuple[int, str], list[tuple[float, bool]]] = {}
    for station_id, door_id, code in session.execute(part_q):
        start = PSD_START[station_id]
        lives.setdefault((station_id, code), []).extend(part_lives(start, records.get((station_id, door_id, code), []), today))

    return {key: calibrate(years, lives.get(key, [])) for key, years in formula.items()}
