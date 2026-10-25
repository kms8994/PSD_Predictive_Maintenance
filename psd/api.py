"""동해선 PSD 교체주기 API (MVP_계획.md 4단계).

    uvicorn psd.api:app --reload
"""
from __future__ import annotations

import io
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

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
    reference_climate,
    single_cause_cycle,
)
from psd.db import get_session
from psd.excel_io import SIDE_LABEL_TO_CODE, ExcelFormatError, parse_upload
from psd.models import CycleResult, Door, Part, ReplacementPart, ReplacementRecord, Station
from psd.schemas import ReplacementRecordIn
from psd.status import (
    UnitStatus,
    all_unit_statuses,
    counts,
    judge_status,
    next_due_date,
    station_unit_statuses,
    to_unit_status,
    worst,
)

app = FastAPI(title="동해선 PSD 교체주기 API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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
    return period_average_af(daily_af(g, ref))[aws_stn]


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
        "due": u.due,
        "days_left": u.days_left,
        "status": u.status,
    }


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


@app.get("/api/stations")
def list_stations(db: Session = Depends(get_db)):
    by_station: dict[int, list[UnitStatus]] = {}
    for u in all_unit_statuses(db):  # 역마다 따로 조회하지 않고 한 번에 가져와 나눔(N+1 방지)
        by_station.setdefault(u.station_id, []).append(u)

    rows = []
    totals = {"빨강": 0, "노랑": 0, "확인_필요": 0}
    for s in db.execute(select(Station).order_by(Station.id)).scalars():
        c = counts(by_station.get(s.id, []))
        for k in totals:
            totals[k] += c[k]
        rows.append(
            {
                "id": s.id,
                "name": s.name,
                "open_date": s.open_date,
                "observatory": s.observatory_id,
                "aws_station": AWS_STATION_NAMES[AWS_STN_BY_STATION[s.name]],
                "near_water": s.near_water,
                "counts": c,
            }
        )
    return {"stations": rows, "totals": totals}


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
            "service_interval_min": SERVICE_INTERVAL_MIN,  # 참고용, 계산에는 안 씀 — psd/cycle.py 주석 참고
        },
        "psc": _unit_dict(psc) if psc else None,
        "sides": {sid: {"label": label, "doors": sides[sid]} for sid, label in SIDES.items()},
        "counts": counts(statuses),
    }


# ---------- 4-3. 문 ----------


@app.get("/api/doors/{door_id}")
def get_door(door_id: int, db: Session = Depends(get_db)):
    door = db.get(Door, door_id)
    if door is None:
        raise HTTPException(404, "문 없음")
    station = db.get(Station, door.station_id)
    parts = db.execute(select(Part).where(Part.door_id == door_id)).scalars().all()
    units = [to_unit_status(p, _part_cycle(p, db)) for p in parts]
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
    }


# ---------- 4-4. 부품 상세 ----------


@app.get("/api/parts/{part_id}")
def get_part(part_id: int, db: Session = Depends(get_db)):
    part = db.get(Part, part_id, options=[joinedload(Part.station), joinedload(Part.door), joinedload(Part.part_type)])
    if part is None:
        raise HTTPException(404, "부품 없음")
    cycle = _part_cycle(part, db)
    unit = to_unit_status(part, cycle)

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
        stations.append({"id": s.id, "name": s.name, "observatory": AWS_STATION_NAMES[aws_stn], "weather": obs_climate, "cycles": cycles})
    return {"stations": stations, "reference": climate["ref"]}


@app.get("/api/weather-impact/simulate")
def simulate_weather(station_id: int, d_temp: float = 0.0, d_rh: float = 0.0, d_dt: float = 0.0, db: Session = Depends(get_db)):
    """한 역의 10년 날씨를 기온 d_temp·습도 d_rh·일교차 d_dt만큼 통째로 바꿔서 다시 계산한다.

    실제 저장된 계산(기간 평균 가속 계수)과 같은 방식을 써야 "지금 날씨" 결과와 비교할 수 있다.
    """
    station = db.get(Station, station_id)
    if station is None:
        raise HTTPException(404, "역 없음")
    af = _shifted_period_af(AWS_STN_BY_STATION[station.name], d_temp, d_rh, d_dt)
    dcm = dcm_cycle(af)
    ifm = single_cause_cycle(af["hum"], IFM_WEIGHT_RANGE)
    motor = single_cause_cycle(af["mot"], MOTOR_WEIGHT_RANGE)
    to_dict = lambda c: {"base_years": round(c.base_years, 2), "lo_years": round(c.lo_years, 2), "hi_years": round(c.hi_years, 2), "judgment": c.judgment}
    return {
        "station_id": station_id,
        "change": {"d_temp": d_temp, "d_rh": d_rh, "d_dt": d_dt},
        "cycles": {"DCM": to_dict(dcm), "IFM": to_dict(ifm), "MOTOR": to_dict(motor)},
    }


# ---------- 4-6. 교체 물량 ----------


def _quarter(d: date) -> tuple[int, int]:
    return d.year, (d.month - 1) // 3 + 1


def _quarter_label(yq: tuple[int, int]) -> str:
    return f"{yq[0]}년 {yq[1]}분기"


def _next_quarter(yq: tuple[int, int], n: int) -> tuple[int, int]:
    idx = yq[0] * 4 + (yq[1] - 1) + n
    return idx // 4, idx % 4 + 1


@app.get("/api/replacement-volume")
def replacement_volume(years_ahead: int = 5, db: Session = Depends(get_db)):
    today = date.today()
    start_q = _quarter(today)
    window = {_next_quarter(start_q, i) for i in range(years_ahead * 4 + 1)}

    table: dict[tuple[int, int], dict[str, int]] = {q: {p["code"]: 0 for p in PART_TYPES} for q in window}
    check: dict[str, int] = {p["code"]: 0 for p in PART_TYPES}

    cycle_map = _cycle_map(db)  # 부품마다 DB를 오가지 않도록 역 x 부품종류 계산 결과를 한 번만 읽음
    parts = db.execute(select(Part)).scalars().all()
    for part in parts:
        cycle = _require_cycle(cycle_map, part.station_id, part.part_type_code)
        due = next_due_date(part.install_date, cycle.base_years)
        days_left = (due - today).days
        status = judge_status(days_left, part.has_record)
        if status == "확인_필요":
            check[part.part_type_code] += 1
            continue
        d = today if days_left < 0 else due
        cycle_days = max(1, round(cycle.base_years * 365.25))  # 0 이하면 무한 루프라 최소 1일
        while True:
            q = _quarter(d)
            if q not in table:
                break
            table[q][part.part_type_code] += 1
            d = d + timedelta(days=cycle_days)

    quarters = sorted(window)
    return {
        "current_quarter": _quarter_label(start_q),
        "check": {"counts": check, "total": sum(check.values())},
        "quarters": [
            {"label": _quarter_label(q), "counts": table[q], "total": sum(table[q].values())}
            for q in quarters
        ],
    }


# ---------- 4-7. 교체 기록 입력 ----------


def _resolve_parts(db: Session, station_id: int, door_id: int | None, part_type_codes: list[str]) -> dict[str, Part]:
    """역·문·부품이 실제 있는지 확인하고 코드→Part 매핑을 돌려준다. 단건 입력과 엑셀 업로드가
    같이 쓴다 — 역·문 해석(이름→id)은 호출하는 쪽에서 미리 끝내고 와야 한다."""
    station = db.get(Station, station_id)
    if station is None:
        raise HTTPException(404, "역 없음")
    if door_id is not None:
        door = db.get(Door, door_id)
        if door is None or door.station_id != station_id:
            raise HTTPException(404, "그 역에 그 문이 없음")

    parts = {
        p.part_type_code: p
        for p in db.execute(
            select(Part).where(
                Part.station_id == station_id,
                Part.door_id == door_id,
                Part.part_type_code.in_(part_type_codes),
            )
        ).scalars()
    }
    missing = set(part_type_codes) - set(parts)
    if missing:
        raise HTTPException(400, f"그 역·문에 없는 부품 종류: {sorted(missing)}")
    return parts


def _insert_replacement_record(
    db: Session,
    station_id: int,
    door_id: int | None,
    replaced_on: date,
    reason: str,
    part_type_codes: list[str],
    created_by: str | None,
    parts: dict[str, Part],
) -> ReplacementRecord:
    """레코드 1건 + 교체 부품들을 세션에 올린다(커밋은 호출하는 쪽에서 — 엑셀 업로드는
    여러 건을 한 트랜잭션으로 묶어야 해서)."""
    record = ReplacementRecord(
        replaced_on=replaced_on,
        station_id=station_id,
        door_id=door_id,
        reason=reason,
        created_by=created_by,
    )
    db.add(record)
    db.flush()

    for code in part_type_codes:
        db.add(ReplacementPart(record_id=record.id, part_type_code=code))
        part = parts[code]
        if not part.has_record or replaced_on > part.install_date:
            part.install_date = replaced_on
        part.has_record = True

    return record


@app.post("/api/replacement-records", status_code=201)
def create_replacement_record(payload: ReplacementRecordIn, db: Session = Depends(get_db)):
    parts = _resolve_parts(db, payload.station_id, payload.door_id, payload.part_type_codes)
    record = _insert_replacement_record(
        db, payload.station_id, payload.door_id, payload.replaced_on, payload.reason,
        payload.part_type_codes, payload.created_by, parts,
    )
    db.commit()

    return {
        "record_id": record.id,
        "replaced_on": record.replaced_on,
        "updated_parts": [
            {
                "part_id": parts[code].id,
                "part_type_code": code,
                "next_due": next_due_date(parts[code].install_date, _part_cycle(parts[code], db).base_years),
            }
            for code in payload.part_type_codes
        ],
    }


@app.post("/api/replacement-records/upload", status_code=201)
async def upload_replacement_records(file: UploadFile, db: Session = Depends(get_db)):
    """교체(고장) 기록을 엑셀로 한 번에 여러 건 올린다(열 형식은 psd/excel_io.py).

    한 행이라도 틀리면 아무것도 저장하지 않는다(all-or-nothing) — 절반만 들어가면 어디까지
    들어갔는지 헷갈리고, 엑셀을 고쳐서 다시 올리기도 더 어려워진다. 그래서 전체 행을 먼저
    검사(역·문·부품이 실제 있는지)하고, 문제 있는 행을 전부 모아 한 번에 알려준다.
    """
    content = await file.read()
    try:
        parsed = parse_upload(content)
    except ExcelFormatError as e:
        raise HTTPException(400, str(e))

    station_by_name = {s.name: s for s in db.execute(select(Station)).scalars()}
    errors: list[str] = []
    resolved = []
    for row in parsed:
        station = station_by_name.get(row.station_name)
        if station is None:
            errors.append(f"{row.row_no}행: 역 이름을 못 찾음: {row.station_name!r}")
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
            door = db.execute(
                select(Door).where(
                    Door.station_id == station.id, Door.side == side_code,
                    Door.car == car, Door.position == position,
                )
            ).scalar_one_or_none()
            if door is None:
                errors.append(f"{row.row_no}행: {row.station_name} {row.side_label} {row.door_no} 문을 못 찾음")
                continue
            door_id = door.id

        part_codes = [NAME_TO_CODE[name] for name in row.part_names]
        try:
            parts = _resolve_parts(db, station.id, door_id, part_codes)
        except HTTPException as e:
            errors.append(f"{row.row_no}행: {e.detail}")
            continue

        resolved.append((row, station.id, door_id, part_codes, parts))

    if errors:
        raise HTTPException(400, {"message": f"{len(errors)}건 오류 — 아무것도 저장하지 않았습니다", "errors": errors})

    records = [
        _insert_replacement_record(db, station_id, door_id, row.replaced_on, row.reason_code, part_codes, row.created_by, parts)
        for row, station_id, door_id, part_codes, parts in resolved
    ]
    db.commit()

    return {"inserted": len(records), "record_ids": [r.id for r in records]}


# ---------- 4-8. 보고서 출력 (2026-10-25 사용자 지시) ----------


@app.get("/api/report")
def download_report(station_id: int | None = None, db: Session = Depends(get_db)):
    """지금 화면에 보이는 상태를 엑셀로 내려받는다. station_id 없으면 24개 역 전체."""
    if station_id is not None:
        station = db.get(Station, station_id)
        if station is None:
            raise HTTPException(404, "역 없음")
        statuses = station_unit_statuses(db, station_id)
        filename = f"PSD_보고서_{station.name}_{date.today():%Y%m%d}.xlsx"
    else:
        statuses = all_unit_statuses(db)
        filename = f"PSD_보고서_전체_{date.today():%Y%m%d}.xlsx"

    station_names = {s.id: s.name for s in db.execute(select(Station)).scalars()}
    door_labels = {d.id: d.label for d in db.execute(select(Door)).scalars()}

    rows = [
        {
            "역": station_names[u.station_id],
            "문": door_labels.get(u.door_id, "역 공통(종합제어반)"),
            "부품": NAME_BY_CODE[u.part_type_code],
            "설치일": u.install_date.isoformat(),
            "권장 주기(년)": round(u.cycle.base_years, 2),
            "주기 범위(년)": f"{u.cycle.lo_years:.1f}-{u.cycle.hi_years:.1f}",
            "판정": u.cycle.judgment,
            "다음 교체 예정일": u.due.isoformat(),
            "남은 기간(일)": u.days_left,
            "상태": u.status,
            "교체 기록 있음": "예" if u.has_record else "아니오",
        }
        for u in sorted(statuses, key=lambda u: (station_names[u.station_id], u.door_id or 0, u.part_type_code))
    ]

    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False)
    buf.seek(0)
    # 코드 리뷰로 찾은 버그: HTTP 헤더는 latin-1만 되는데 파일명에 한글이 들어가면
    # StreamingResponse 생성 자체가 UnicodeEncodeError로 죽는다. RFC 5987대로
    # filename*=UTF-8''(퍼센트 인코딩)을 같이 주고, 예전 클라이언트용 filename=은
    # ASCII만 남긴 이름으로 둔다.
    ascii_name = "PSD_report.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"},
    )


# ---------- 화면 (설계_v2.md 6-3, MVP_계획.md 5단계) ----------
# 목업(목업/동해선_PSD_교체주기_목업.html)은 그대로 두고, 실제 API로 연결한 화면만 여기서 띄운다.

WEB_DIR = Path(__file__).resolve().parent / "web"
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
