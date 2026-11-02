"""동해선 PSD 교체주기 API (MVP_계획.md 4단계).

    uvicorn psd.api:app --reload
"""
from __future__ import annotations

import io
import math
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, joinedload

from psd.calibrate import PRIOR_FAILURES, PSD_START, Calibrated, calibration_map
from psd.catalog import NAME_TO_CODE, PART_TYPES, SIDES
from psd.cycle import (
    IFM_WEIGHT_RANGE,
    MOTOR_WEIGHT_RANGE,
    SERVICE_INTERVAL_MIN,
    STATIONS,
    AWS_STATION_NAMES,
    daily_af,
    dcm_cycle,
    load_aws_weather,
    observatory_climate,
    period_average_af,
    period_average_af_for,
    reference_climate,
    single_cause_cycle,
)
from psd.db import get_session
from psd.excel_io import (
    REASON_CODE_TO_LABEL,
    SIDE_LABEL_TO_CODE,
    ExcelFormatError,
    ParsedRow,
    parse_upload,
    rows_to_excel_bytes,
    template_bytes,
)
from psd.models import CycleResult, Door, Part, ReplacementPart, ReplacementRecord, Station, UploadBatch
from psd.repeat import REPEAT_MIN_FAILURES, REPEAT_P_MAX, REPEAT_WINDOW_YEARS, DoorRepeat, find_repeat_doors, window_start
from psd.status import (
    RANK,
    _joined_parts,
    UnitStatus,
    all_unit_statuses,
    counts,
    judge_status,
    next_due_date,
    station_unit_statuses,
    to_unit_status,
    worst,
)
from psd.sync import RecordSig, diff_records

app = FastAPI(title="동해선 PSD 교체주기 API")
# CORS는 열지 않는다: 화면(psd/web)은 같은 주소에서 나가서 필요 없고, allow_origins=["*"]로 두면 아무 웹사이트나
# 이 API의 기록·보고서를 읽어 갈 수 있었다(2026-11-02 QA). 로그인은 아직 없다 — 배포 전에 넣는다

NAME_BY_CODE = {p["code"]: p["name"] for p in PART_TYPES}
WEATHER_PART_CODES = ["DCM", "IFM", "MOTOR"]
AWS_STN_BY_STATION = {s.name: s.aws_stn for s in STATIONS}  # 역 이름 -> 가장 가까운 AWS 지점 코드

# 고장 원인별 열화 속도·비중 기본값(4단계_계산공식_근거조사.md 1·3장). 부품 상세의 "근거"
# 표에 쓴다. 근거가 없어 균등 기본값 + 범위로 두며, 역 구분은 없다 — AWS 실측 습도가 이미
# 역(정확히는 aws_stn)마다 다르게 들어가므로(2-1) 비중까지 역별로 나눌 근거는 더 없다.
CAUSES = {
    "DCM": [("콘덴서 노화", "기온", "cap", "25% (10–50%)"), ("납땜 열피로", "일교차", "sol", "25% (10–50%)"), ("습도 부식", "습도·기온", "hum", "25% (10–50%)"), ("날씨 무관", "—", None, "25% (10–50%)")],
    "IFM": [("접점 부식", "습도·기온", "hum", "50% (20–80%)"), ("날씨 무관", "—", None, "50%")],
    "MOTOR": [("권선 절연 노화", "기온", "mot", "30% (10–50%)"), ("날씨 무관(베어링 등)", "—", None, "70%")],
}


def get_db():
    db = get_session()
    try:
        yield db
    finally:
        db.close()


@lru_cache
def _weather_df():
    return load_aws_weather()


@lru_cache
def _climate() -> dict:
    """기후 요약(표시용 장기 평균). 계산은 _af_by_stn()의 기간 평균 가속 계수를 쓴다."""
    weather = _weather_df()
    return {
        "ref": reference_climate(weather),
        "obs": observatory_climate(weather),
        "start": weather["date"].min().date(),
        "end": weather["date"].max().date(),
    }


@lru_cache
def _af_by_stn() -> dict:
    """AWS 지점별 기간 평균 가속 계수(psd/cycle.py의 compute_all과 같은 방식).

    부품 상세의 "근거" 표는 이 값을 써야 "이 역의 권장 주기"(DB에 저장된 값)와 맞는다.
    station_climate의 장기 평균 T/RH/DT를 AF 공식에 한 번만 넣는 것과는 다른 결과가 나온다
    (가속 계수는 비선형이라 날마다 계산해 평균 내는 것과 평균 날씨로 한 번 계산하는 것이 다름).
    """
    weather = _weather_df()
    ref = reference_climate(weather)
    return period_average_af(daily_af(weather, ref))


def _shifted_period_af(aws_stn: str, d_temp: float, d_rh: float, d_dt: float) -> dict[str, float]:
    """한 AWS 지점의 날씨를 통째로 (기온·습도·일교차) 바꿔서 다시 기간 평균 가속 계수를 낸다.

    시뮬레이터용. 실제 ta_max가 없는 가상 날씨라 Tmax는 기온 + 일교차/2로 근사한다.
    """
    ref = _climate()["ref"]  # 이미 캐시된 기준 기후를 그대로 씀(슬라이더 드래그마다 다시 구하지 않음)
    g = _weather_df()
    g = g[g["stn"] == aws_stn].copy()
    g["ta_avg"] = g["ta_avg"] + d_temp
    g["hm_avg"] = (g["hm_avg"] + d_rh).clip(lower=0.0, upper=100.0)
    g["dtr"] = (g["dtr"] + d_dt).clip(lower=0.5)
    g["ta_max"] = g["ta_avg"] + g["dtr"] / 2
    return period_average_af_for(daily_af(g, ref), aws_stn)


def _cal_dict(cal: Calibrated | None) -> dict | None:
    if cal is None:
        return None
    return {
        "formula_years": round(cal.formula_years, 2),
        "years": round(cal.years, 2),
        "raw_years": round(cal.raw_years, 2),  # 10년 제한 전 계산값
        "capped": cal.capped,
        "failures": cal.failures,
        "units": cal.units,
        "observed_years": round(cal.observed_years, 1),
        "data_share": round(cal.data_share, 2),
        "prior_failures": PRIOR_FAILURES,
    }


def _unit_dict(u: UnitStatus) -> dict:
    return {
        "part_id": u.part_id,
        "part_type_code": u.part_type_code,
        "part_name": NAME_BY_CODE[u.part_type_code],
        "install_date": u.install_date,
        "has_record": u.has_record,
        "cycle": {
            "base_years": round(u.cycle.base_years, 2),
            "lo_years": round(u.cycle.lo_years, 2),
            "hi_years": round(u.cycle.hi_years, 2),
            "judgment": u.cycle.judgment,
        },
        "cycle_years": round(u.cycle_years, 2),  # 예정일에 쓴 주기(기록 반영)
        "calibration": _cal_dict(u.cal),
        "due": u.due,
        "days_left": u.days_left,
        "status": u.status,
    }


@app.get("/api/today")
def get_today():
    """상태·예정일을 계산하는 기준일(서버의 오늘). 화면 위쪽에 "기준일"로 늘 보여 준다."""
    return {"today": date.today()}


@app.get("/api/part-types")
def list_part_types():
    """부품 종류 목록(코드·이름·단위·날씨 계산 여부). 화면의 고정 표시용 — 계산값은 아님."""
    return {"part_types": PART_TYPES}


def _cycle_map(db: Session) -> dict[tuple[int, str], CycleResult]:
    """역 x 부품 종류의 계산 결과를 한 번에 읽어 온다(24 x 7 = 168건, 요청마다 한 번만).

    이걸 안 쓰고 부품마다 db.get(CycleResult, ...)을 부르면(교체 물량처럼 수천 건을
    도는 곳에서) 역-부품종류 조합 수만큼 DB를 오간다.
    """
    return {(cr.station_id, cr.part_type_code): cr for cr in db.execute(select(CycleResult)).scalars()}


def _require_cycle(cycle_map: dict[tuple[int, str], CycleResult], station_id: int, code: str) -> CycleResult:
    cycle = cycle_map.get((station_id, code))
    if cycle is None:
        raise HTTPException(500, f"계산 결과 없음: station={station_id} part={code}. "
                                  f"python scripts/compute_cycles.py 를 먼저 실행해야 함")
    return cycle


def _part_cycle(part: Part, db: Session) -> CycleResult:
    cycle = db.get(CycleResult, (part.station_id, part.part_type_code))
    if cycle is None:
        raise HTTPException(500, f"계산 결과 없음: station={part.station_id} part={part.part_type_code}. "
                                  f"python scripts/compute_cycles.py 를 먼저 실행해야 함")
    return cycle


# ---------- 4-1. 노선 ----------


def _part_summary(units: list[UnitStatus], doors: dict[int, Door] | None = None) -> dict[str, dict]:
    """부품 종류마다 가장 급한 상태와 그 예정일, 상태별 개수. 역 화면의 "부품별 상태" 표와 노선 격자에 쓴다.

    doors(문 id → Door)를 주면 부품 하나하나(어느 문인지·상태·예정일)도 급한 순으로 넣는다 — 역 화면에서
    "위험 12개가 어느 문인지" 작업 지시서에 옮겨 적으려고(2026-11-02). 노선 목록(24개 역)에는 안 넣는다.
    """
    by_code: dict[str, list[UnitStatus]] = {}
    for u in units:
        by_code.setdefault(u.part_type_code, []).append(u)
    out = {}
    for code, us in by_code.items():
        w = worst(us)
        out[code] = {
            "status": w.status,
            "due": w.due,
            "days_left": w.days_left,
            "part_id": w.part_id,
            "total": len(us),
            "counts": counts(us),
        }
        if doors is not None:
            out[code]["units"] = [
                {
                    "part_id": u.part_id,
                    "side": doors[u.door_id].side if u.door_id else None,
                    "no": f"{doors[u.door_id].car}-{doors[u.door_id].position}" if u.door_id else None,
                    "status": u.status,
                    "due": u.due,
                    "days_left": u.days_left,
                }
                for u in sorted(us, key=lambda u: (RANK[u.status], u.days_left, u.door_id or 0))
            ]
    return out


@app.get("/api/stations")
def list_stations(db: Session = Depends(get_db)):
    by_station: dict[int, list[UnitStatus]] = {}
    statuses = all_unit_statuses(db)
    for u in statuses:  # 역마다 따로 조회하지 않고 한 번에 가져와 나눔(N+1 방지)
        by_station.setdefault(u.station_id, []).append(u)

    repeat_by_station: dict[int, int] = {}
    for r in _repeat_map(db).values():
        if r.flagged:
            repeat_by_station[r.station_id] = repeat_by_station.get(r.station_id, 0) + 1

    rows = []
    totals = {"빨강": 0, "노랑": 0, "확인_필요": 0}
    for s in db.execute(select(Station).order_by(Station.id)).scalars():
        units = by_station.get(s.id, [])
        c = counts(units)
        for k in totals:
            totals[k] += c[k]
        soonest = min(units, key=lambda u: u.days_left) if units else None
        rows.append(
            {
                "id": s.id,
                "name": s.name,
                "open_date": s.open_date,
                "observatory": s.observatory_id,
                "aws_station": AWS_STATION_NAMES[AWS_STN_BY_STATION[s.name]],
                "near_water": s.near_water,
                "in_service": s.in_service,
                "counts": c,
                "repeat_doors": repeat_by_station.get(s.id, 0),  # 반복 고장 의심 문 수(psd/repeat.py) — 노선도·역 목록 표시용
                # 역 목록에서 "다음 교체가 언제인지"를 보여 주려고 — 개통 전 역은 부품이 없어 None
                "next": {
                    "due": soonest.due,
                    "days_left": soonest.days_left,
                    "part_type_code": soonest.part_type_code,
                } if soonest else None,
                "parts": _part_summary(units),
            }
        )
    # 교체 기록이 한 번도 없는 부품은 예정일을 설치일(PSD 사용개시일)로 추정한다 — 관리자가 예정일을 얼마나 믿을지 보라고 비율을 같이 준다
    data_quality = {"parts": len(statuses), "no_record": sum(1 for u in statuses if not u.has_record)}
    return {"stations": rows, "totals": totals, "data_quality": data_quality}


# ---------- 4-1b. 관리 현황(교체 예정 목록, 최근 교체 기록) ----------


@app.get("/api/work-items")
def work_items(within_days: int = 730, db: Session = Depends(get_db)):
    """관리자가 챙길 교체 대상을 역 x 부품 종류 x 예정일로 묶어 급한 순서로 돌려준다.

    같은 날 개통한 역의 같은 부품은 문 32개가 같은 날 예정일을 맞기 때문에, 부품 하나씩
    늘어놓으면 똑같은 줄이 수백 개 나온다. 실제로도 한 역의 같은 부품은 한 번에 교체하므로
    묶어서 보여 준다. 초록이 아닌 부품(위험·확인 필요·주의)은 기간과 상관없이 항상 넣는다.
    """
    if within_days < 0:
        raise HTTPException(400, "within_days는 0 이상이어야 함")
    stations = {s.id: s.name for s in db.execute(select(Station)).scalars()}
    door_labels = {d.id: d.label for d in db.execute(select(Door)).scalars()}

    groups: dict[tuple[int, str, date, str], list[UnitStatus]] = {}
    soonest: UnitStatus | None = None
    for u in all_unit_statuses(db):
        if soonest is None or u.days_left < soonest.days_left:
            soonest = u
        if u.status == "초록" and u.days_left > within_days:
            continue
        groups.setdefault((u.station_id, u.part_type_code, u.due, u.status), []).append(u)

    items = []
    for (station_id, code, due, status), units in groups.items():
        first = units[0]
        items.append(
            {
                "station_id": station_id,
                "station_name": stations[station_id],
                "part_type_code": code,
                "part_name": NAME_BY_CODE[code],
                "due": due,
                "days_left": first.days_left,
                "status": status,
                "count": len(units),
                # 1개뿐이면 화면이 바로 그 부품 상세로 갈 수 있게 id와 위치를 같이 준다
                "part_id": first.part_id if len(units) == 1 else None,
                "location": (door_labels[first.door_id] if first.door_id else "역 공통(종합제어반)")
                if len(units) == 1 else None,
            }
        )
    items.sort(key=lambda x: (RANK[x["status"]], x["days_left"], x["station_id"], x["part_type_code"]))

    return {
        "today": date.today(),
        "within_days": within_days,
        "items": items,
        "part_count": sum(x["count"] for x in items),
        "soonest": {
            "station_name": stations[soonest.station_id],
            "part_name": NAME_BY_CODE[soonest.part_type_code],
            "due": soonest.due,
            "days_left": soonest.days_left,
        } if soonest else None,
    }


@app.get("/api/replacement-records")
def recent_replacement_records(
    limit: int = 20,
    offset: int = 0,
    order: str = "created",
    station_id: int | None = None,
    part: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    reason: str | None = None,
    db: Session = Depends(get_db),
):
    """교체 기록 목록(교체 기록 탭의 기록 찾기, 현황의 "최근 교체 기록").

    order=created(기본): 입력 순서 최신부터. 업로드가 제대로 들어갔는지 확인하는 용도.
    한 번에 올린 기록은 입력 시각이 같아서, 그 안에서는 교체일 최신부터 늘어놓는다.
    order=replaced: 교체일 최신부터.
    station_id·part(부품 종류 코드)·교체일 기간(date_from~date_to, 양 끝 포함)·reason(사유 코드)으로 거르고
    offset으로 쪽을 넘긴다. total은 거른 뒤의 건수.
    """
    if order not in ("created", "replaced"):
        raise HTTPException(400, "order는 created 또는 replaced여야 함")
    if reason is not None and reason not in REASON_CODE_TO_LABEL:
        raise HTTPException(400, f"reason은 {', '.join(REASON_CODE_TO_LABEL)} 중 하나여야 함")
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    newest_replaced = (ReplacementRecord.replaced_on.desc(), ReplacementRecord.id.desc())
    sort = newest_replaced if order == "replaced" else (ReplacementRecord.created_at.desc(), *newest_replaced)
    where = []
    if station_id is not None:
        where.append(ReplacementRecord.station_id == station_id)
    if part:
        where.append(ReplacementRecord.id.in_(select(ReplacementPart.record_id).where(ReplacementPart.part_type_code == part)))
    if date_from:
        where.append(ReplacementRecord.replaced_on >= date_from)
    if date_to:
        where.append(ReplacementRecord.replaced_on <= date_to)
    if reason:
        where.append(ReplacementRecord.reason == reason)
    records = (
        db.execute(
            select(ReplacementRecord)
            .options(joinedload(ReplacementRecord.station), joinedload(ReplacementRecord.door),
                     joinedload(ReplacementRecord.parts))
            .where(*where)
            .order_by(*sort)
            .offset(offset)
            .limit(limit)
        )
        .unique()
        .scalars()
        .all()
    )
    total = db.scalar(select(func.count(ReplacementRecord.id)).where(*where))
    return {
        "total": total,
        "records": [
            {
                "id": r.id,
                "replaced_on": r.replaced_on,
                "station_id": r.station_id,
                "station_name": r.station.name,
                "location": r.door.label if r.door else "역 공통(종합제어반)",
                "reason": r.reason,
                "parts": [NAME_BY_CODE[p.part_type_code] for p in r.parts],
                "created_by": r.created_by,
                "created_at": r.created_at,
            }
            for r in records
        ],
    }


# ---------- 4-1c. 반복 고장 문 (psd/repeat.py) ----------


def _repeat_map(db: Session, station_id: int | None = None) -> dict[int, DoorRepeat]:
    """문 id → 반복 고장 통계. station_id를 주면 그 역만(같은 역끼리만 비교하므로 결과는 같다)."""
    today = date.today()
    q = (
        select(ReplacementRecord.door_id, ReplacementRecord.replaced_on, ReplacementRecord.station_id, ReplacementPart.part_type_code)
        .join(ReplacementPart, ReplacementPart.record_id == ReplacementRecord.id)
        .where(ReplacementRecord.reason == "fail", ReplacementRecord.door_id.is_not(None),
               ReplacementRecord.replaced_on >= window_start(today))
    )
    dq = select(Door.id, Door.station_id)
    if station_id is not None:
        q = q.where(ReplacementRecord.station_id == station_id)
        dq = dq.where(Door.station_id == station_id)
    by_record: dict[tuple[int, date], list[str]] = {}
    for door_id, day, _, code in db.execute(q):
        by_record.setdefault((door_id, day), []).append(code)
    doors_by_station: dict[int, list[int]] = {}
    for door_id, st in db.execute(dq):
        doors_by_station.setdefault(st, []).append(door_id)
    failures = [(door_id, day, codes) for (door_id, day), codes in by_record.items()]
    return find_repeat_doors(failures, doors_by_station, today)


def _repeat_dict(r: DoorRepeat) -> dict:
    return {
        "failures": r.failures,
        "peer_mean": round(r.peer_mean, 2) if r.peer_mean is not None else None,
        "ratio": round(r.ratio, 1) if r.ratio is not None else None,
        "p_value": round(r.p_value, 4) if r.p_value is not None else None,
        "flagged": r.flagged,
        "by_part": [{"code": c, "name": NAME_BY_CODE[c], "count": n} for c, n in r.by_part.most_common()],
        "last": r.last,
    }


REPEAT_RULE = {"window_years": REPEAT_WINDOW_YEARS, "min_failures": REPEAT_MIN_FAILURES, "p_max": REPEAT_P_MAX}


@app.get("/api/repeat-doors")
def repeat_doors(db: Session = Depends(get_db)):
    """같은 역 다른 문보다 유난히 고장이 잦은 문(반복 고장 의심). 우연일 확률이 낮은 순."""
    doors = {d.id: d for d in db.execute(select(Door).options(joinedload(Door.station))).scalars()}
    flagged = sorted((r for r in _repeat_map(db).values() if r.flagged), key=lambda r: (r.p_value, -r.failures))
    return {
        "today": date.today(),
        "since": window_start(date.today()),
        "rule": REPEAT_RULE,
        "doors_checked": len(doors),
        "doors": [
            {
                "door_id": r.door_id,
                "station_id": r.station_id,
                "station_name": doors[r.door_id].station.name,
                "side": doors[r.door_id].side,
                "label": doors[r.door_id].label,
                "no": f"{doors[r.door_id].car}-{doors[r.door_id].position}",
                **_repeat_dict(r),
            }
            for r in flagged
        ],
    }


# ---------- 4-2. 역 ----------


@app.get("/api/stations/{station_id}")
def get_station(station_id: int, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if station is None:
        raise HTTPException(404, "역 없음")

    statuses = station_unit_statuses(db, station_id)
    by_door: dict[int | None, list[UnitStatus]] = {}
    psc = None
    for u in statuses:
        if u.door_id is None:
            psc = u
        else:
            by_door.setdefault(u.door_id, []).append(u)

    doors = db.execute(select(Door).where(Door.station_id == station_id).order_by(Door.side, Door.car, Door.position)).scalars().all()
    repeat = _repeat_map(db, station_id)
    sides: dict[str, list[dict]] = {side: [] for side in SIDES}
    for d in doors:
        door_units = by_door.get(d.id)
        if not door_units:
            raise HTTPException(
                500, f"문 {d.id}의 계산 결과 없음. python scripts/compute_cycles.py 를 먼저 실행해야 함"
            )
        w = worst(door_units)
        sides[d.side].append(
            {
                "id": d.id,
                "car": d.car,
                "position": d.position,
                "label": d.label,
                "no": f"{d.car}-{d.position}",
                "worst_status": w.status,
                "worst_days_left": w.days_left,
                "repeat": repeat[d.id].flagged,  # 반복 고장 의심(psd/repeat.py)
            }
        )

    return {
        "station": {
            "id": station.id,
            "name": station.name,
            "open_date": station.open_date,
            "observatory": station.observatory_id,
            "aws_station": AWS_STATION_NAMES[AWS_STN_BY_STATION[station.name]],
            "near_water": station.near_water,
            "in_service": station.in_service,
            "psd_install_date": PSD_START[station.id],  # 역 화면에 개통일과 따로 보여 준다(개통 2016인데 PSD는 2022인 역이 있다)
            "service_interval_min": SERVICE_INTERVAL_MIN,  # 참고용, 계산에는 안 씀 — psd/cycle.py 주석 참고
        },
        "psc": _unit_dict(psc) if psc else None,
        "sides": {sid: {"label": label, "doors": sides[sid]} for sid, label in SIDES.items()},
        "counts": counts(statuses),
        "parts": _part_summary(statuses, {d.id: d for d in doors}),
        "repeat_rule": REPEAT_RULE,
    }


# ---------- 4-3. 문 ----------


@app.get("/api/doors/{door_id}")
def get_door(door_id: int, db: Session = Depends(get_db)):
    door = db.get(Door, door_id)
    if door is None:
        raise HTTPException(404, "문 없음")
    station = db.get(Station, door.station_id)
    parts = db.execute(select(Part).where(Part.door_id == door_id)).scalars().all()
    cal = calibration_map(db, station_id=door.station_id)
    units = [to_unit_status(p, _part_cycle(p, db), cal=cal.get((p.station_id, p.part_type_code))) for p in parts]
    units.sort(key=lambda u: list(NAME_BY_CODE).index(u.part_type_code) if u.part_type_code in NAME_BY_CODE else 0)
    return {
        "door": {
            "id": door.id,
            "station_id": station.id,
            "station_name": station.name,
            "side": door.side,
            "side_label": SIDES[door.side],
            "car": door.car,
            "position": door.position,
            "label": door.label,
            "no": f"{door.car}-{door.position}",
        },
        "units": [_unit_dict(u) for u in units],
        "repeat": _repeat_dict(_repeat_map(db, door.station_id)[door.id]),
        "repeat_rule": REPEAT_RULE,
    }


# ---------- 4-4. 부품 상세 ----------


@app.get("/api/parts/{part_id}")
def get_part(part_id: int, db: Session = Depends(get_db)):
    part = db.get(Part, part_id, options=[joinedload(Part.station), joinedload(Part.door), joinedload(Part.part_type)])
    if part is None:
        raise HTTPException(404, "부품 없음")
    cycle = _part_cycle(part, db)
    cal = calibration_map(db, station_id=part.station_id).get((part.station_id, part.part_type_code))
    unit = to_unit_status(part, cycle, cal=cal)

    weather_basis = None
    if part.part_type.weather_sensitive:
        climate = _climate()
        aws_stn = AWS_STN_BY_STATION[part.station.name]
        sc, ref = climate["obs"][aws_stn], climate["ref"]
        af = _af_by_stn()[aws_stn]  # 기간 평균 가속 계수 — "이 역의 권장 주기"와 같은 계산
        weather_basis = {
            "observatory": AWS_STATION_NAMES[aws_stn],
            "station_climate": sc,
            "reference_climate": ref,
            "weather_period": {"start": climate["start"], "end": climate["end"]},
            "causes": [
                {"name": name, "variable": var, "af": round(af[key], 2) if key else 1.0, "weight_default": weight}
                for name, var, key, weight in CAUSES[part.part_type_code]
            ],
        }

    history = (
        db.execute(
            select(ReplacementRecord, ReplacementPart)
            .join(ReplacementPart, ReplacementPart.record_id == ReplacementRecord.id)
            .where(
                ReplacementPart.part_type_code == part.part_type_code,
                ReplacementRecord.station_id == part.station_id,
                ReplacementRecord.door_id == part.door_id,
            )
            .order_by(ReplacementRecord.replaced_on.desc())
        )
        .all()
    )

    return {
        "part": {
            "id": part.id,
            "part_type_code": part.part_type_code,
            "part_name": part.part_type.name,
            "station_id": part.station_id,
            "station_name": part.station.name,
            "door_id": part.door_id,
            "door_label": part.door.label if part.door else "역 공통(종합제어반)",
        },
        "cycle": {
            "base_years": round(cycle.base_years, 2),
            "lo_years": round(cycle.lo_years, 2),
            "hi_years": round(cycle.hi_years, 2),
            "judgment": cycle.judgment,
        },
        "cycle_years": round(unit.cycle_years, 2),
        "calibration": _cal_dict(cal),
        "install_date": unit.install_date,
        "due": unit.due,
        "days_left": unit.days_left,
        "status": unit.status,
        "weather_basis": weather_basis,
        "history": [
            {"replaced_on": rec.replaced_on, "reason": rec.reason, "created_by": rec.created_by}
            for rec, _rp in history
        ],
    }


# ---------- 4-5. 날씨 영향 ----------


@app.get("/api/weather-impact")
def weather_impact(db: Session = Depends(get_db)):
    climate = _climate()
    cycle_map = _cycle_map(db)
    stations = []
    for s in db.execute(select(Station).order_by(Station.id)).scalars():
        aws_stn = AWS_STN_BY_STATION[s.name]
        obs_climate = climate["obs"][aws_stn]
        cycles = {}
        for code in WEATHER_PART_CODES:
            cr = _require_cycle(cycle_map, s.id, code)
            cycles[code] = {"base_years": round(cr.base_years, 2), "lo_years": round(cr.lo_years, 2), "hi_years": round(cr.hi_years, 2), "judgment": cr.judgment}
        stations.append({"id": s.id, "name": s.name, "in_service": s.in_service, "observatory": AWS_STATION_NAMES[aws_stn], "weather": obs_climate, "cycles": cycles})
    return {"stations": stations, "reference": climate["ref"]}


@app.get("/api/weather-impact/simulate")
def simulate_weather(station_id: int, d_temp: float = 0.0, d_rh: float = 0.0, d_dt: float = 0.0, db: Session = Depends(get_db)):
    """한 역의 10년 날씨를 기온 d_temp·습도 d_rh·일교차 d_dt만큼 통째로 바꿔서 다시 계산한다.

    실제 저장된 계산(기간 평균 가속 계수)과 같은 방식을 써야 "지금 날씨" 결과와 비교할 수 있다.
    """
    if not all(math.isfinite(x) for x in (d_temp, d_rh, d_dt)):
        raise HTTPException(400, "d_temp·d_rh·d_dt는 숫자여야 함(nan·inf 안 됨)")
    station = db.get(Station, station_id)
    if station is None:
        raise HTTPException(404, "역 없음")
    aws_stn = AWS_STN_BY_STATION[station.name]
    to_dict =lambda c: {"base_years": round(c.base_years, 2), "lo_years": round(c.lo_years, 2), "hi_years": round(c.hi_years, 2), "judgment": c.judgment}
    return {
        "station_id": station_id,
        "change": {"d_temp": d_temp, "d_rh": d_rh, "d_dt": d_dt},
        "cycles": {k: to_dict(c) for k, c in _weather_cycles(aws_stn, d_temp, d_rh, d_dt).items()},
        # 비교 기준도 같은 근사(Tmax = 기온 + 일교차/2)로 다시 계산한다. 저장된 값(실제 Tmax)과 비교하면
        # 슬라이더를 0에 두어도 차이가 조금 생겼다
        "baseline": {k: to_dict(c) for k, c in _weather_cycles(aws_stn, 0.0, 0.0, 0.0).items()},
    }


@lru_cache(maxsize=512)
def _weather_cycles(aws_stn: str, d_temp: float, d_rh: float, d_dt: float) -> dict:
    af = _shifted_period_af(aws_stn, d_temp, d_rh, d_dt)
    return {"DCM": dcm_cycle(af), "IFM": single_cause_cycle(af["hum"], IFM_WEIGHT_RANGE), "MOTOR": single_cause_cycle(af["mot"], MOTOR_WEIGHT_RANGE)}


# 민감도 표에서 "날씨 한 칸"으로 쓰는 변화량. 해안·내륙 지점 습도 차이가 5%p 정도라(psd/cycle.py 머리말) 습도는 5%p로 둔다
SENSITIVITY_STEPS = {"rh": 5.0, "temp": 1.0, "dt": 1.0}


@app.get("/api/weather-impact/sensitivity")
def weather_sensitivity():
    """날씨가 한 칸(습도 +5%p, 기온 +1°C, 일교차 +1°C) 바뀌면 부품 주기가 몇 년 바뀌는지.

    운영 중인 역이 쓰는 AWS 지점마다 계산해서 평균 낸다(지점마다 조금씩 다르다).
    """
    stns = sorted({s.aws_stn for s in STATIONS if s.in_service})
    out = {code: {} for code in WEATHER_PART_CODES}
    for var, step in SENSITIVITY_STEPS.items():
        shift = {"rh": (0.0, step, 0.0), "temp": (step, 0.0, 0.0), "dt": (0.0, 0.0, step)}[var]
        for code in WEATHER_PART_CODES:
            deltas = [_weather_cycles(stn, *shift)[code].base_years - _weather_cycles(stn, 0.0, 0.0, 0.0)[code].base_years for stn in stns]
            out[code][var] = round(sum(deltas) / len(deltas), 3)
    return {"steps": SENSITIVITY_STEPS, "parts": out}


# ---------- 4-6. 교체 물량 ----------


def _quarter(d: date) -> tuple[int, int]:
    return d.year, (d.month - 1) // 3 + 1


def _quarter_label(yq: tuple[int, int]) -> str:
    return f"{yq[0]}년 {yq[1]}분기"


def _next_quarter(yq: tuple[int, int], n: int) -> tuple[int, int]:
    idx = yq[0] * 4 + (yq[1] - 1) + n
    return idx // 4, idx % 4 + 1


def _next_month(ym: tuple[int, int], n: int) -> tuple[int, int]:
    idx = ym[0] * 12 + (ym[1] - 1) + n
    return idx // 12, idx % 12 + 1


def _plan_counts(db: Session, today: date, buckets: list, bucket_of) -> tuple[dict, dict, dict[str, int]]:
    """앞으로의 교체 예정을 기간(buckets)마다 부품 종류별·역 x 부품 종류별로 센다.

    교체한 뒤 다음 주기 예정일도 기간 안이면 다시 센다. 이미 지난(위험) 부품은 지금 기간에 넣는다.
    확인 필요(기록 없이 예정일 지남)는 언제 바꿀지 알 수 없어 따로 센다.
    역별은 {(역 id, 부품 코드): 개수}로 준다 — 역 합계만 주면 그 역에 무슨 부품을 챙겨 가야 할지 몰랐다(2026-11-02).
    """
    window = set(buckets)
    by_part = {b: {p["code"]: 0 for p in PART_TYPES} for b in buckets}
    by_station: dict = {b: {} for b in buckets}
    check: dict[str, int] = {p["code"]: 0 for p in PART_TYPES}

    cal_map = calibration_map(db, today)
    for part, _cycle in _joined_parts(db):  # 부품 열 값 캐시(psd/status.py) — 부품을 ORM 객체로 다시 읽지 않는다
        years = cal_map[(part.station_id, part.part_type_code)].years
        due = next_due_date(part.install_date, years)
        days_left = (due - today).days
        status = judge_status(days_left, part.has_record)
        if status == "확인_필요":
            check[part.part_type_code] += 1
            continue
        d = today if days_left < 0 else due
        cycle_days = max(1, round(years * 365.25))  # 0 이하면 무한 루프라 최소 1일
        while True:
            b = bucket_of(d)
            if b not in window:
                break
            by_part[b][part.part_type_code] += 1
            key = (part.station_id, part.part_type_code)
            by_station[b][key] = by_station[b].get(key, 0) + 1
            d = d + timedelta(days=cycle_days)
    return by_part, by_station, check


@app.get("/api/replacement-volume")
def replacement_volume(years_ahead: int = 5, db: Session = Depends(get_db)):
    today = date.today()
    start_q = _quarter(today)
    quarters = [_next_quarter(start_q, i) for i in range(years_ahead * 4 + 1)]
    table, _, check = _plan_counts(db, today, quarters, _quarter)
    return {
        "current_quarter": _quarter_label(start_q),
        "check": {"counts": check, "total": sum(check.values())},
        "quarters": [
            {"label": _quarter_label(q), "counts": table[q], "total": sum(table[q].values())}
            for q in quarters
        ],
    }


# 교체 계획 화면의 기간: 12개월은 월별, 3년은 분기별, 5년은 연도별. 지금 달·분기·해(남은 기간만)를 맨 앞에 넣는다
PLAN_SPANS = ("12m", "3y", "5y")


def _station_rows(by_key: dict[tuple[int, str], int], names: dict[int, str]) -> list[dict]:
    """{(역 id, 부품 코드): 개수} → 역마다 합계와 부품별 개수, 많은 역부터."""
    rows: dict[int, dict] = {}
    for (sid, code), n in by_key.items():
        r = rows.setdefault(sid, {"id": sid, "name": names[sid], "count": 0, "parts": {}})
        r["count"] += n
        r["parts"][code] = n
    return sorted(rows.values(), key=lambda r: (-r["count"], r["id"]))


@app.get("/api/replacement-plan")
def replacement_plan(span: str = "12m", db: Session = Depends(get_db)):
    """기간마다 교체 예정 부품 수(부품 종류별·역별). 자재 발주와 작업 배분, 몰리는 때 앞당겨 나눌 역 고르기에 쓴다."""
    return _replacement_plan(span, db)


def _replacement_plan(span: str, db: Session) -> dict:
    if span not in PLAN_SPANS:
        raise HTTPException(400, f"span은 {', '.join(PLAN_SPANS)} 중 하나여야 함")
    today = date.today()
    if span == "12m":
        buckets = [_next_month((today.year, today.month), i) for i in range(13)]
        bucket_of, label, unit = (lambda d: (d.year, d.month)), (lambda b: f"{b[0]}년 {b[1]}월"), "month"
    elif span == "3y":
        buckets = [_next_quarter(_quarter(today), i) for i in range(13)]
        bucket_of, label, unit = _quarter, _quarter_label, "quarter"
    else:
        buckets = [(today.year + i, 0) for i in range(6)]
        bucket_of, label, unit = (lambda d: (d.year, 0)), (lambda b: f"{b[0]}년"), "year"
    by_part, by_station, check = _plan_counts(db, today, buckets, bucket_of)
    names = {s.id: s.name for s in db.execute(select(Station)).scalars()}
    # 계획은 예정 교체만 센다. 돌발 고장 대비 예비품은 지난 1년 고장 교체 수를 참고로 같이 준다 — 3년 계획에 타이밍벨트가
    # 0개로 나와도 실제로는 고장이 나고 있어, 0만 보고 발주하면 예비품이 모자랐다(2026-11-02)
    recent_failures = {p["code"]: 0 for p in PART_TYPES}
    for (_sid, code), n in _recent_failures(db, today).items():
        recent_failures[code] += n
    return {
        "today": today,
        "span": span,
        "unit": unit,
        "check": {"counts": check, "total": sum(check.values())},
        "recent_failures": recent_failures,
        "buckets": [
            {
                "label": label(b),
                "year": b[0],
                "sub": b[1],
                "counts": by_part[b],
                "total": sum(by_part[b].values()),
                "stations": _station_rows(by_station[b], names),
            }
            for b in buckets
        ],
    }


@app.get("/api/replacement-plan/export")
def export_replacement_plan(span: str = "12m", db: Session = Depends(get_db)):
    """교체 계획을 엑셀로: "부품별"(부품 x 기간), "역별"(역 x 기간), "역x부품"(기간·역·부품·수량 한 줄씩 — 발주서에 거르기 좋게)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    plan = _replacement_plan(span, db)
    B = plan["buckets"]
    labels = [b["label"] for b in B]
    wb = Workbook()

    def sheet(ws, header: list[str], rows: list[list], widths: list[int]):
        ws.append(header)
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1D5EA8")
        for r in rows:
            ws.append(r)
        ws.freeze_panes = "B2"
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

    by_part = wb.active
    by_part.title = "부품별"
    rf = plan["recent_failures"]
    part_rows = [[p["name"], *[b["counts"][p["code"]] for b in B], sum(b["counts"][p["code"]] for b in B), rf[p["code"]]] for p in PART_TYPES]
    part_rows.append(["합계", *[b["total"] for b in B], sum(b["total"] for b in B), sum(rf.values())])
    sheet(by_part, ["부품", *labels, "합계", "참고: 지난 1년 고장 교체(예비품)"], part_rows, [16, *[12] * len(B), 10, 18])
    for cell in by_part[by_part.max_row]:
        cell.font = Font(bold=True)

    stations: dict[int, dict] = {}
    for i, b in enumerate(B):
        for s in b["stations"]:
            stations.setdefault(s["id"], {"name": s["name"], "n": [0] * len(B)})["n"][i] = s["count"]
    st_rows = [[v["name"], *v["n"], sum(v["n"])] for _sid, v in sorted(stations.items())]
    sheet(wb.create_sheet("역별"), ["역", *labels, "합계"], st_rows, [12, *[12] * len(B), 10])

    long_rows = [[b["label"], s["name"], NAME_BY_CODE[code], n]
                 for b in B for s in sorted(b["stations"], key=lambda s: s["id"])
                 for code in (p["code"] for p in PART_TYPES) if (n := s["parts"].get(code))]
    ws = wb.create_sheet("역x부품")
    sheet(ws, ["기간", "역", "부품", "수량"], long_rows, [14, 12, 16, 8])
    ws.auto_filter.ref = ws.dimensions

    if plan["check"]["total"]:
        note = wb.create_sheet("확인 필요")
        sheet(note, ["부품", "개수(계획에서 뺌 — 기록 없이 예정일이 지나 교체 여부부터 확인)"],
              [[NAME_BY_CODE[c], n] for c, n in plan["check"]["counts"].items() if n], [16, 60])

    buf = io.BytesIO()
    wb.save(buf)
    name = {"12m": "12개월", "3y": "3년", "5y": "5년"}[span]
    today = plan["today"]
    return _xlsx_response(buf.getvalue(), f"PSD_교체계획_{name}_{today:%Y%m%d}.xlsx", f"PSD_plan_{span}_{today:%Y%m%d}.xlsx")


# ---------- 4-7. 교체 기록 올리기 (엑셀이 원본, psd/sync.py) ----------
# 2026-11-01: 한 건 입력 화면·API는 없앴다. 현장 정비기록(엑셀)이 유일한 원본이어야 다음 업로드 때
# "엑셀에 없는 기록"이 헷갈리지 않는다. 고칠 때도 엑셀을 고쳐 다시 올린다.


def _resolve_rows(db: Session, parsed: list[ParsedRow]) -> list[tuple[ParsedRow, RecordSig]]:
    """엑셀 행의 역·문·부품 이름을 DB id로 바꾼다.

    한 행이라도 틀리면 아무것도 하지 않는다(all-or-nothing) — 절반만 들어가면 어디까지 들어갔는지
    헷갈리고, 엑셀을 고쳐서 다시 올리기도 더 어려워진다. 그래서 틀린 행을 전부 모아 한 번에 알려준다.
    """
    # 행마다 Door·Station·Part를 따로 조회하면 업로드 1건에 수천 번 DB 왕복이 생긴다(코드 리뷰로 찾은
    # 버그 — 더미데이터 1,230건을 한 번에 올린 적이 있다, MVP_계획.md 2026-10-25). 전체 역·문·부품을
    # 미리 한 번씩만 읽어 와 딕셔너리로 찾는다(N+1 방지).
    station_by_name = {s.name: s for s in db.execute(select(Station)).scalars()}
    door_by_key = {(d.station_id, d.side, d.car, d.position): d for d in db.execute(select(Door)).scalars()}
    part_keys = {(p.station_id, p.door_id, p.part_type_code) for p in db.execute(select(Part)).scalars()}

    errors: list[str] = []
    resolved = []
    for row in parsed:
        station = station_by_name.get(row.station_name)
        if station is None:
            errors.append(f"{row.row_no}행: 역 이름을 못 찾음: {row.station_name!r}")
            continue
        # PSD 사용개시 전 날짜(2026을 2016으로 치는 오타 등)가 들어가면 설치일이 그날로 바뀌어 부품이 수천 일 지난
        # "위험"이 됐다 — 주기 보정(psd/calibrate.py part_lives)은 이런 날짜를 빼지만 설치일 계산은 안 뺐다(2026-11-02 QA)
        if row.replaced_on < PSD_START[station.id]:
            errors.append(f"{row.row_no}행: 교체일 {row.replaced_on}이 {station.name} PSD 사용개시일"
                          f"({PSD_START[station.id]})보다 앞섭니다")
            continue

        door_id = None
        if row.side_label:
            side_code = SIDE_LABEL_TO_CODE[row.side_label]
            try:
                car_s, position_s = row.door_no.split("-")
                car, position = int(car_s), int(position_s)
            except Exception:
                errors.append(f"{row.row_no}행: 문번호 형식이 잘못됨(예: '1-1'): {row.door_no!r}")
                continue
            door = door_by_key.get((station.id, side_code, car, position))
            if door is None:
                errors.append(f"{row.row_no}행: {row.station_name} {row.side_label} {row.door_no} 문을 못 찾음")
                continue
            door_id = door.id

        part_codes = sorted({NAME_TO_CODE[name] for name in row.part_names})
        missing = [c for c in part_codes if (station.id, door_id, c) not in part_keys]
        if missing:
            errors.append(f"{row.row_no}행: 그 역·문에 없는 부품 종류: {missing}")
            continue

        resolved.append((row, RecordSig(station.id, door_id, row.replaced_on, row.reason_code, tuple(part_codes))))

    if errors:
        raise HTTPException(400, {"message": f"{len(errors)}건 오류 — 아무것도 저장하지 않았습니다", "errors": errors})
    return resolved


def _sig_of(r: ReplacementRecord) -> RecordSig:
    return RecordSig(r.station_id, r.door_id, r.replaced_on, r.reason, tuple(sorted(p.part_type_code for p in r.parts)))


def _record_dict(r: ReplacementRecord) -> dict:
    """지우는 기록을 그대로 적어 둔다(UploadBatch.removed — 되돌릴 때 이걸로 다시 넣는다)."""
    return {
        "station_id": r.station_id, "door_id": r.door_id, "replaced_on": r.replaced_on.isoformat(), "reason": r.reason,
        "parts": sorted(p.part_type_code for p in r.parts), "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None, "batch_id": r.batch_id,
    }


def _add_record(db: Session, sig: RecordSig, created_by: str | None, batch_id: int | None, created_at: datetime | None = None) -> None:
    record = ReplacementRecord(replaced_on=sig.replaced_on, station_id=sig.station_id, door_id=sig.door_id,
                               reason=sig.reason, created_by=created_by, batch_id=batch_id)
    if created_at is not None:
        record.created_at = created_at
    record.parts = [ReplacementPart(part_type_code=code) for code in sig.parts]
    db.add(record)


def _delete_record(db: Session, r: ReplacementRecord) -> None:
    for p in r.parts:
        db.delete(p)
    db.delete(r)


def _recompute_parts(db: Session, part_keys: set[tuple[int, int | None, str]]) -> None:
    """부품의 설치일(마지막 교체일 — 점검은 빼고)과 기록 여부를 남은 기록으로 다시 맞춘다.

    기록을 넣고 지운 뒤 한 번만 부른다. 넣은 기록마다 설치일을 앞뒤로 비교해 바꾸던 예전 방식은
    지우기가 함께 있으면 순서에 따라 틀릴 수 있어서, 끝난 상태에서 한 번에 다시 센다.
    점검(이상 없음)은 부품을 바꾼 게 아니라 설치일엔 안 들어가고 기록 여부(has_record)만 켠다.
    """
    if not part_keys:
        return
    station_ids = {sid for sid, _door, _code in part_keys}
    agg = {
        (sid, door_id, code): (last, n)
        for sid, door_id, code, last, n in db.execute(
            select(ReplacementRecord.station_id, ReplacementRecord.door_id, ReplacementPart.part_type_code,
                   func.max(case((ReplacementRecord.reason != "check", ReplacementRecord.replaced_on))),
                   func.count(ReplacementRecord.id))
            .join(ReplacementPart, ReplacementPart.record_id == ReplacementRecord.id)
            .where(ReplacementRecord.station_id.in_(station_ids))
            .group_by(ReplacementRecord.station_id, ReplacementRecord.door_id, ReplacementPart.part_type_code)
        )
    }
    # 업로드 두 개가 겹쳐 같은 부품을 동시에 바꾸지 않게 부품 행을 잠근다(SELECT ... FOR UPDATE).
    # populate_existing=True가 있어야 이 세션에 캐시된 옛 값이 아니라 DB의 최신 값을 받는다.
    parts = db.execute(
        select(Part).where(Part.station_id.in_(station_ids)).with_for_update().execution_options(populate_existing=True)
    ).scalars()
    for p in parts:
        key = (p.station_id, p.door_id, p.part_type_code)
        if key in part_keys:
            last, n = agg.get(key, (None, 0))
            p.install_date = last or PSD_START[p.station_id]
            p.has_record = n > 0


class _Labels:
    """화면에 보여 줄 기록 한 줄(역 이름·문 이름·부품 이름)."""

    def __init__(self, db: Session):
        self.station = {s.id: s.name for s in db.execute(select(Station)).scalars()}
        self.door = {d.id: d.label for d in db.execute(select(Door)).scalars()}

    def row(self, sig: RecordSig, created_by: str | None = None) -> dict:
        return {
            "replaced_on": sig.replaced_on, "station_id": sig.station_id, "station_name": self.station[sig.station_id],
            "location": self.door[sig.door_id] if sig.door_id else "역 공통(종합제어반)", "reason": sig.reason,
            "parts": [NAME_BY_CODE[c] for c in sig.parts], "created_by": created_by,
        }


SHOW_ROWS = 300  # 비교 결과에 줄 단위로 보여 줄 최대 수(건수는 전부 센다)
MISSING_WARN = 20  # 엑셀에서 빠진 기록이 이만큼 넘으면 "다른 파일을 고른 것 같다"고 경고


def _sync(db: Session, content: bytes, file_name: str, delete_missing: bool, uploaded_by: str | None = None) -> dict:
    """엑셀을 DB와 비교하고 엑셀에 맞춘다(flush까지만). 커밋·롤백은 부르는 쪽이 정한다 —
    비교 화면·정비기록 시연은 롤백, "반영하기"는 커밋.

    - 엑셀에만 있음 → 넣는다 / 같은 역·문·날짜인데 사유·부품이 다름 → 고친다
    - DB에만 있음(비교 범위 안) → delete_missing일 때만 지운다(화면에서 체크해야 지움)
    - DB에 같은 기록이 여러 번 → 하나만 남긴다
    """
    try:
        parsed = parse_upload(content)
    except ExcelFormatError as e:
        raise HTTPException(400, str(e))
    resolved = _resolve_rows(db, parsed)
    sigs = [sig for _row, sig in resolved]
    created_by: dict[RecordSig, str | None] = {}
    for row, sig in resolved:
        created_by.setdefault(sig, row.created_by)

    station_ids = {s.station_id for s in sigs}
    scope_from, scope_to = min(s.replaced_on for s in sigs), max(s.replaced_on for s in sigs)
    in_scope = (
        db.execute(
            select(ReplacementRecord).options(joinedload(ReplacementRecord.parts))
            .where(ReplacementRecord.station_id.in_(station_ids), ReplacementRecord.replaced_on.between(scope_from, scope_to))
        ).unique().scalars().all()
    )
    by_id = {r.id: r for r in in_scope}
    d = diff_records(sigs, [(r.id, _sig_of(r)) for r in in_scope])

    removed_ids = d.db_duplicates + [rid for rid, _sig in d.changed] + (d.missing if delete_missing else [])
    to_delete = [by_id[rid] for rid in removed_ids]
    to_insert = d.new + [sig for _rid, sig in d.changed]
    part_keys = ({(s.station_id, s.door_id, c) for s in to_insert for c in s.parts}
                 | {(r.station_id, r.door_id, p.part_type_code) for r in to_delete for p in r.parts})
    keys = {(sid, code) for sid, _door, code in part_keys}

    labels = _Labels(db)
    old_row = lambda rid: labels.row(_sig_of(by_id[rid]), by_id[rid].created_by)  # noqa: E731
    result = {
        "file": file_name,
        # 범위의 양 끝이 엑셀 몇 행인지도 준다 — 이번 달 파일에 옛날 정정 한 줄만 섞여도 범위가 몇 년으로 넓어져
        # "엑셀에 없는 기록"이 수백 건 뜨는데, 왜 그런지 찾을 수 있게(2026-11-02)
        "scope": {"from": scope_from, "to": scope_to, "stations": len(station_ids),
                  "from_row": min(row.row_no for row, sig in resolved if sig.replaced_on == scope_from),
                  "to_row": min(row.row_no for row, sig in resolved if sig.replaced_on == scope_to)},
        "counts": {"new": len(d.new), "changed": len(d.changed), "missing": len(d.missing), "db_duplicates": len(d.db_duplicates),
                   "same": d.same, "file_duplicates": d.file_duplicates},
        "delete_missing": delete_missing,
        "missing_warn": len(d.missing) >= MISSING_WARN,
        "new": [labels.row(s, created_by[s]) for s in d.new[:SHOW_ROWS]],
        "changed": [{"before": old_row(rid), "after": labels.row(s, created_by[s])} for rid, s in d.changed[:SHOW_ROWS]],
        "missing": [old_row(rid) for rid in d.missing[:SHOW_ROWS]],
        "inserted": len(to_insert),
        "replaced": sum(1 for s in to_insert if s.reason != "check"),
        "checked": sum(1 for s in to_insert if s.reason == "check"),
        "deleted": len(to_delete) - len(d.changed),
        "batch_id": None,
        "changes": [],
    }
    if not to_insert and not to_delete:
        return result  # 바뀌는 게 없으면 이력도 남기지 않는다

    before = _group_snapshot(db, keys)
    batch = UploadBatch(
        file_name=file_name[:255], scope_from=scope_from, scope_to=scope_to, station_count=len(station_ids),
        inserted=len(d.new), updated=len(d.changed), deleted=result["deleted"], removed=[_record_dict(r) for r in to_delete],
        uploaded_by=(uploaded_by or "").strip()[:50] or None,
    )
    db.add(batch)
    db.flush()
    for r in to_delete:
        _delete_record(db, r)
    for s in to_insert:
        _add_record(db, s, created_by[s], batch.id)
    db.flush()
    _recompute_parts(db, part_keys)
    db.flush()
    result["batch_id"] = batch.id
    result["changes"] = _changes(db, before, _group_snapshot(db, keys))
    return result


def _group_snapshot(db: Session, keys: set[tuple[int, str]]) -> dict[tuple[int, str], dict]:
    """역 x 부품 종류 묶음마다 지금의 주기·예정일·상태 개수를 찍어 둔다.

    기록을 넣기 전과 넣은 뒤에 한 번씩 불러 "무엇이 바뀌었는지"를 화면에 보여 주는 데 쓴다.
    값을 dict로 복사해 두므로, 그 뒤에 부품 행이 바뀌어도(설치일 갱신) 찍어 둔 값은 그대로다.
    """
    today = date.today()
    out: dict[tuple[int, str], dict] = {}
    cycle_map = _cycle_map(db)
    for station_id in {s for s, _ in keys}:
        cal = calibration_map(db, today, station_id, use_cache=False)  # 커밋 전 상태를 봐야 한다
        units: dict[str, list[UnitStatus]] = {}
        for p in db.execute(select(Part).where(Part.station_id == station_id)).scalars():
            if (station_id, p.part_type_code) in keys:
                key = (station_id, p.part_type_code)
                units.setdefault(p.part_type_code, []).append(to_unit_status(p, cycle_map[key], today, cal[key]))
        for code, us in units.items():
            c = cal[(station_id, code)]
            soon = min(us, key=lambda u: u.days_left)
            out[(station_id, code)] = {
                "years": round(c.years, 2),
                "formula_years": round(c.formula_years, 2),
                "failures": c.failures,
                "units": c.units,
                "parts": len(us),
                "next_due": soon.due,
                "days_left": soon.days_left,
                "counts": counts(us),
            }
    return out


def _changes(db: Session, before: dict, after: dict) -> list[dict]:
    names = {s.id: s.name for s in db.execute(select(Station)).scalars()}
    rows = [
        {
            "station_id": sid,
            "station_name": names[sid],
            "part_type_code": code,
            "part_name": NAME_BY_CODE[code],
            "before": before[(sid, code)],
            "after": after[(sid, code)],
        }
        for sid, code in before
        if (sid, code) in after
    ]
    rows.sort(key=lambda r: -abs(r["after"]["years"] - r["before"]["years"]))
    return rows


@app.post("/api/replacement-records/compare")
async def compare_replacement_records(file: UploadFile, delete_missing: bool = Form(False), db: Session = Depends(get_db)):
    """올리기 전에 DB와 비교만 한다(무엇이 들어가고·고쳐지고·빠지는지, 교체주기가 어떻게 바뀌는지). 저장하지 않는다."""
    try:
        result = _sync(db, await file.read(), file.filename or "업로드.xlsx", delete_missing)
    finally:
        db.rollback()
    del result["batch_id"]  # 롤백해서 없는 이력
    return result


@app.post("/api/replacement-records/sync", status_code=201)
async def sync_replacement_records(file: UploadFile, delete_missing: bool = Form(False), uploaded_by: str | None = Form(None),
                                   db: Session = Depends(get_db)):
    """비교 화면의 "반영하기": 엑셀에 맞춰 DB를 고치고 업로드 이력을 남긴다(열 형식은 psd/excel_io.py).
    uploaded_by는 화면에서 적은 올린 사람 이름(선택) — 이력에서 누가 올렸는지 보려고(2026-11-02)."""
    result = _sync(db, await file.read(), file.filename or "업로드.xlsx", delete_missing, uploaded_by)
    db.commit()
    return result


# 정비기록 시연 탭에서 쓰는 샘플(scripts/make_sample_records.py로 만든 파일)
SAMPLE_RECORDS_PATH = Path(__file__).resolve().parent.parent / "data" / "dummy" / "정비기록_샘플_329건.xlsx"


def _sample_bytes() -> bytes:
    if not SAMPLE_RECORDS_PATH.exists():
        raise HTTPException(404, f"샘플 파일이 없음: {SAMPLE_RECORDS_PATH.name} (scripts/make_sample_records.py로 만든다)")
    return SAMPLE_RECORDS_PATH.read_bytes()


@app.get("/api/sample-records")
def sample_records():
    """정비기록 시연 탭의 입력: 샘플 엑셀의 행을 그대로 보여 준다."""
    try:
        parsed = parse_upload(_sample_bytes())
    except ExcelFormatError as e:
        raise HTTPException(400, str(e))
    return {
        "file": SAMPLE_RECORDS_PATH.name,
        "rows": [
            {"station_name": r.station_name, "side_label": r.side_label, "door_no": r.door_no, "replaced_on": r.replaced_on,
             "reason": REASON_CODE_TO_LABEL[r.reason_code], "parts": ", ".join(r.part_names), "created_by": r.created_by}
            for r in parsed
        ],
    }


@app.post("/api/sample-records/preview")
def preview_sample_records(db: Session = Depends(get_db)):
    """샘플을 올렸다고 치고 결과만 계산한 뒤 롤백한다 — 몇 번을 눌러도 DB는 그대로다.
    샘플엔 그 역·기간의 초기 데이터도 들어 있어 "엑셀에 없는 기록"이 없지만, 만에 하나 있어도 지우지 않는다(delete_missing=False)."""
    try:
        result = _sync(db, _sample_bytes(), SAMPLE_RECORDS_PATH.name, delete_missing=False)
    finally:
        db.rollback()
    del result["batch_id"]
    return result


# ---------- 업로드 이력·되돌리기 ----------


def _latest_active_batch_id(db: Session) -> int | None:
    return db.scalar(select(func.max(UploadBatch.id)).where(UploadBatch.undone_at.is_(None)))


@app.get("/api/upload-batches")
def list_upload_batches(limit: int = 20, db: Session = Depends(get_db)):
    """업로드 이력(최근 것부터). 되돌리기는 아직 안 되돌린 것 중 가장 최근 것만 된다(can_undo)."""
    limit = max(1, min(limit, 100))
    latest = _latest_active_batch_id(db)
    batches = db.execute(select(UploadBatch).order_by(UploadBatch.id.desc()).limit(limit)).scalars().all()
    return {
        "total": db.scalar(select(func.count(UploadBatch.id))),
        "batches": [
            {
                "id": b.id, "file_name": b.file_name, "uploaded_at": b.uploaded_at,
                "scope_from": b.scope_from, "scope_to": b.scope_to, "station_count": b.station_count,
                "inserted": b.inserted, "updated": b.updated, "deleted": b.deleted,
                "undone_at": b.undone_at, "can_undo": b.id == latest, "uploaded_by": b.uploaded_by,
            }
            for b in batches
        ],
    }


@app.post("/api/upload-batches/{batch_id}/undo")
def undo_upload_batch(batch_id: int, db: Session = Depends(get_db)):
    """업로드 한 번을 되돌린다: 그때 넣은 기록은 지우고, 그때 지우거나 고친 기록은 원래대로 다시 넣는다.

    나중 업로드가 이 업로드의 기록을 또 고쳤을 수 있어서, 가장 최근 업로드부터 차례로만 되돌린다.
    """
    batch = db.execute(select(UploadBatch).where(UploadBatch.id == batch_id).with_for_update()).scalar_one_or_none()
    if batch is None:
        raise HTTPException(404, "업로드 이력 없음")
    if batch.undone_at is not None:
        raise HTTPException(409, "이미 되돌린 업로드입니다")
    if batch.id != _latest_active_batch_id(db):
        raise HTTPException(409, "가장 최근 업로드부터 차례로 되돌릴 수 있습니다")

    added = (
        db.execute(select(ReplacementRecord).options(joinedload(ReplacementRecord.parts)).where(ReplacementRecord.batch_id == batch.id))
        .unique().scalars().all()
    )
    restore = [
        (RecordSig(x["station_id"], x["door_id"], date.fromisoformat(x["replaced_on"]), x["reason"], tuple(x["parts"])), x)
        for x in batch.removed
    ]
    part_keys = ({(r.station_id, r.door_id, p.part_type_code) for r in added for p in r.parts}
                 | {(s.station_id, s.door_id, c) for s, _x in restore for c in s.parts})

    for r in added:
        _delete_record(db, r)
    for s, x in restore:
        _add_record(db, s, x["created_by"], x["batch_id"], datetime.fromisoformat(x["created_at"]) if x["created_at"] else None)
    db.flush()
    _recompute_parts(db, part_keys)
    batch.undone_at = datetime.now()
    db.commit()
    return {"file": batch.file_name, "deleted": len(added), "restored": len(restore)}


@app.get("/api/replacement-records/template")
def download_upload_template():
    """엑셀 업로드 양식: 첫 시트 "기록"은 열 이름만, 둘째 시트 "작성 예시"에 예시 3행.
    업로드는 첫 시트만 읽어서, 예시를 안 지워도 가짜 기록이 들어가지 않는다."""
    examples = [
        {"역": "거제해맞이", "방향": "부전 방면", "문번호": "1-1", "교체일": "2026-10-15", "사유": "고장", "부품": "IFM 릴레이", "입력자": "홍길동"},
        {"역": "거제해맞이", "방향": "", "문번호": "", "교체일": "2026-10-15", "사유": "예방 교체", "부품": "종합제어반 릴레이", "입력자": "홍길동"},
        {"역": "거제해맞이", "방향": "부전 방면", "문번호": "1-2", "교체일": "2026-10-16", "사유": "점검(이상 없음)", "부품": "전체", "입력자": "홍길동"},
    ]
    return _xlsx_response(template_bytes(examples), "PSD_교체기록_업로드양식.xlsx", "PSD_upload_template.xlsx")


@app.get("/api/replacement-records/export")
def export_replacement_records(db: Session = Depends(get_db)):
    """DB의 교체 기록 전부를 업로드 양식 그대로 내려받는다(교체일 순). 원본 엑셀을 잃어버렸을 때
    이 파일을 원본 삼아 이어 쓰면 되고, 그대로 다시 올리면 "변경 없음"이 나온다."""
    records = (
        db.execute(
            select(ReplacementRecord)
            .options(joinedload(ReplacementRecord.station), joinedload(ReplacementRecord.door), joinedload(ReplacementRecord.parts))
            .order_by(ReplacementRecord.replaced_on, ReplacementRecord.id)
        ).unique().scalars().all()
    )
    rows = [
        {
            "역": r.station.name,
            "방향": SIDES[r.door.side] if r.door else "",
            "문번호": f"{r.door.car}-{r.door.position}" if r.door else "",
            "교체일": r.replaced_on.isoformat(),
            "사유": REASON_CODE_TO_LABEL[r.reason],
            "부품": ",".join(NAME_BY_CODE[c] for c in sorted(p.part_type_code for p in r.parts)),
            "입력자": r.created_by or "",
        }
        for r in records
    ]
    today = date.today().isoformat()
    return _xlsx_response(rows_to_excel_bytes(rows), f"PSD_교체기록_전체_{today}.xlsx", f"PSD_records_{today}.xlsx")


def _xlsx_response(content: bytes, filename: str, ascii_name: str) -> StreamingResponse:
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"},
    )


# ---------- 4-8. 보고서 출력 (2026-10-25 사용자 지시) ----------


def _recent_failures(db: Session, today: date, station_id: int | None = None) -> dict[tuple[int, str], int]:
    """(역, 부품) → 지난 1년 고장으로 바꾼 부품 수. 계획·보고서의 예비품 참고(예정 교체엔 돌발 고장이 없다)."""
    q = (select(ReplacementRecord.station_id, ReplacementPart.part_type_code, func.count())
         .join(ReplacementRecord, ReplacementRecord.id == ReplacementPart.record_id)
         .where(ReplacementRecord.reason == "fail", ReplacementRecord.replaced_on > today - timedelta(days=365))
         .group_by(ReplacementRecord.station_id, ReplacementPart.part_type_code))
    if station_id is not None:
        q = q.where(ReplacementRecord.station_id == station_id)
    return {(sid, code): n for sid, code, n in db.execute(q)}


def _door_positions(db: Session, station_id: int | None = None) -> dict[int, "DoorPos"]:
    from psd.report_common import DoorPos

    q = select(Door) if station_id is None else select(Door).where(Door.station_id == station_id)
    return {d.id: DoorPos(d.side, d.car, d.position) for d in db.execute(q).scalars()}


@app.get("/api/report")
def download_report(station_id: int | None = None, db: Session = Depends(get_db)):
    """교체 보고서(엑셀). station_id 없으면 운영 중인 역 전체. 시트 구성은 psd/report_excel.py."""
    from psd.report_excel import build_report_workbook

    today = date.today()
    if station_id is not None:
        station = db.get(Station, station_id)
        if station is None:
            raise HTTPException(404, "역 없음")
        statuses = station_unit_statuses(db, station_id)
        title, filename = f"{station.name}역 PSD 부품 교체 보고서", f"PSD_보고서_{station.name}_{today:%Y%m%d}.xlsx"
    else:
        statuses = all_unit_statuses(db)
        title, filename = "동해선 PSD 부품 교체 보고서(전체 역)", f"PSD_보고서_전체_{today:%Y%m%d}.xlsx"

    station_names = {s.id: s.name for s in db.execute(select(Station)).scalars() if any(u.station_id == s.id for u in statuses)}
    wb = build_report_workbook(statuses, station_names, _door_positions(db, station_id), PSD_START,
                               _recent_failures(db, today, station_id), title, today)
    buf = io.BytesIO()
    wb.save(buf)
    # 한글 파일명은 filename*=UTF-8''(RFC 5987)로 준다 — HTTP 헤더는 latin-1만 돼서 그냥 넣으면 응답 생성이 죽었다
    return _xlsx_response(buf.getvalue(), filename, "PSD_report.xlsx")


def _station_pdf(db: Session, station: Station) -> bytes:
    """역 1곳의 PSD 부품 교체 현황 보고서(PDF, A4 2쪽). 양식은 psd/report_pdf.py."""
    from psd.report_pdf import DoorInfo, RecordInfo, build_station_pdf

    doors = [
        DoorInfo(d.id, d.side, d.car, d.position, d.label)
        for d in db.execute(select(Door).where(Door.station_id == station.id)).scalars()
    ]
    recent = (
        db.execute(
            select(ReplacementRecord)
            .options(joinedload(ReplacementRecord.door), joinedload(ReplacementRecord.parts))
            .where(ReplacementRecord.station_id == station.id)
            .order_by(ReplacementRecord.replaced_on.desc(), ReplacementRecord.id.desc())
            .limit(6)  # 2쪽 안에 들게(안전문 표 아래)
        )
        .unique()
        .scalars()
        .all()
    )
    total = db.scalar(select(func.count()).select_from(ReplacementRecord).where(ReplacementRecord.station_id == station.id))
    cfg = next(s for s in STATIONS if s.name == station.name)
    return build_station_pdf(
        station_id=station.id,
        station_name=station.name,
        psd_install_date=cfg.psd_install_date or cfg.open_date,
        statuses=station_unit_statuses(db, station.id),
        doors=doors,
        records=[
            RecordInfo(r.replaced_on, r.door.label if r.door else "역 공통", [p.part_type_code for p in r.parts], r.reason, r.created_by)
            for r in recent
        ],
        record_total=total,
        recent_failures={code: n for (_sid, code), n in _recent_failures(db, date.today(), station.id).items()},
    )


def _pdf_response(pdf: bytes, filename: str) -> StreamingResponse:
    # 엑셀 보고서와 같은 이유로 한글 파일명은 filename*(RFC 5987)로만 준다
    return StreamingResponse(
        io.BytesIO(pdf),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=\"PSD_station_report.pdf\"; filename*=UTF-8''{quote(filename)}"},
    )


@app.get("/api/stations/{station_id}/report.pdf")
def download_station_report_pdf(station_id: int, db: Session = Depends(get_db)):
    """역 1곳의 보고서(PDF, A4 2쪽)."""
    station = db.get(Station, station_id)
    if station is None:
        raise HTTPException(404, "역 없음")
    if not station.in_service:
        raise HTTPException(409, "개통 전 역이라 보고서에 넣을 부품이 없음")
    return _pdf_response(_station_pdf(db, station), f"PSD_역보고서_{station.name}_{date.today():%Y%m%d}.pdf")


@app.get("/api/report.pdf")
def download_all_stations_report_pdf(db: Session = Depends(get_db)):
    """운영 중인 역 전부의 보고서를 역 순서대로 한 PDF로 묶는다(역마다 2쪽, 2026-11-01 보고서 형식 고르기)."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for station in db.execute(select(Station).where(Station.in_service).order_by(Station.id)).scalars():
        writer.append(io.BytesIO(_station_pdf(db, station)))
    buf = io.BytesIO()
    writer.write(buf)
    return _pdf_response(buf.getvalue(), f"PSD_역보고서_전체_{date.today():%Y%m%d}.pdf")


# ---------- 화면 (설계_v2.md 6-3, MVP_계획.md 5단계) ----------
# 목업(목업/동해선_PSD_교체주기_목업.html)은 그대로 두고, 실제 API로 연결한 화면만 여기서 띄운다.

WEB_DIR = Path(__file__).resolve().parent / "web"


@app.middleware("http")
async def _revalidate_web(request, call_next):
    """화면 파일은 매번 바뀌었는지 서버에 물어보게 한다(바뀌지 않았으면 304라 가볍다).
    헤더가 없으면 브라우저가 옛 index.html을 그대로 써서, 코드를 고친 뒤에도 강력 새로고침 전까진 옛 화면이 떴다(2026-11-02)."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
