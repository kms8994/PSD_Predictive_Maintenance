"""동해선 PSD 교체주기 API (MVP_계획.md 4단계).

    uvicorn psd.api:app --reload
"""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from psd.catalog import PART_TYPES, SIDES
from psd.cycle import (
    IFM_WEIGHT_RANGE,
    MOTOR_WEIGHT_RANGE,
    OBS_CODE,
    daily_af,
    dcm_cycle,
    load_weather,
    observatory_climate,
    period_average_af,
    reference_climate,
    single_cause_cycle,
)
from psd.db import get_session
from psd.models import CycleResult, Door, Part, ReplacementPart, ReplacementRecord, Station
from psd.schemas import ReplacementRecordIn
from psd.status import UnitStatus, counts, judge_status, next_due_date, station_unit_statuses, to_unit_status, worst

app = FastAPI(title="동해선 PSD 교체주기 API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

NAME_BY_CODE = {p["code"]: p["name"] for p in PART_TYPES}
WEATHER_PART_CODES = ["DCM", "IFM", "MOTOR"]

# 고장 원인별 열화 속도·비중 기본값(4단계_계산공식_근거조사.md 1·3장). 부품 상세의 "근거" 표에 쓴다.
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
    return load_weather()


@lru_cache
def _climate() -> dict:
    """기후 요약(표시용 장기 평균). 계산은 _af_by_obs()의 기간 평균 가속 계수를 쓴다."""
    weather = _weather_df()
    return {
        "ref": reference_climate(weather),
        "obs": observatory_climate(weather),
        "start": weather["date"].min().date(),
        "end": weather["date"].max().date(),
    }


@lru_cache
def _af_by_obs() -> dict:
    """관측소별 기간 평균 가속 계수(psd/cycle.py의 compute_all과 같은 방식).

    부품 상세의 "근거" 표는 이 값을 써야 "이 역의 권장 주기"(DB에 저장된 값)와 맞는다.
    station_climate의 장기 평균 T/RH/DT를 AF 공식에 한 번만 넣는 것과는 다른 결과가 나온다
    (가속 계수는 비선형이라 날마다 계산해 평균 내는 것과 평균 날씨로 한 번 계산하는 것이 다름).
    """
    weather = _weather_df()
    ref = reference_climate(weather)
    return period_average_af(daily_af(weather, ref))


def _shifted_period_af(obs_code: str, d_temp: float, d_rh: float, d_dt: float) -> dict[str, float]:
    """한 관측소의 10년 날씨를 통째로 (기온·습도·일교차) 바꿔서 다시 기간 평균 가속 계수를 낸다.

    시뮬레이터용. 실제 ta_max가 없는 가상 날씨라 Tmax는 기온 + 일교차/2로 근사한다.
    """
    ref = reference_climate(_weather_df())
    g = _weather_df()
    g = g[g["stn"] == obs_code].copy()
    g["ta_avg"] = g["ta_avg"] + d_temp
    g["hm_avg"] = (g["hm_avg"] + d_rh).clip(lower=0.0, upper=100.0)
    g["dtr"] = (g["dtr"] + d_dt).clip(lower=0.5)
    g["ta_max"] = g["ta_avg"] + g["dtr"] / 2
    obs_name = next(name for name, code in OBS_CODE.items() if code == obs_code)
    return period_average_af(daily_af(g, ref))[obs_name]


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


def _part_cycle(part: Part, db: Session) -> CycleResult:
    cycle = db.get(CycleResult, (part.station_id, part.part_type_code))
    if cycle is None:
        raise HTTPException(500, f"계산 결과 없음: station={part.station_id} part={part.part_type_code}. "
                                  f"python scripts/compute_cycles.py 를 먼저 실행해야 함")
    return cycle


# ---------- 4-1. 노선 ----------


@app.get("/api/stations")
def list_stations(db: Session = Depends(get_db)):
    rows = []
    totals = {"빨강": 0, "노랑": 0, "확인_필요": 0}
    for s in db.execute(select(Station).order_by(Station.id)).scalars():
        c = counts(station_unit_statuses(db, s.id))
        for k in totals:
            totals[k] += c[k]
        rows.append(
            {
                "id": s.id,
                "name": s.name,
                "open_date": s.open_date,
                "observatory": s.observatory_id,
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
        w = worst(by_door[d.id])
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
        "station": {"id": station.id, "name": station.name, "open_date": station.open_date, "observatory": station.observatory_id},
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
        obs = part.station.observatory_id
        obs_name = next(name for name, code in OBS_CODE.items() if code == obs)
        sc, ref = climate["obs"][obs_name], climate["ref"]
        af = _af_by_obs()[obs_name]  # 기간 평균 가속 계수 — "이 역의 권장 주기"와 같은 계산
        weather_basis = {
            "observatory": obs_name,
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
    stations = []
    for s in db.execute(select(Station).order_by(Station.id)).scalars():
        obs_name = next(name for name, code in OBS_CODE.items() if code == s.observatory_id)
        obs_climate = climate["obs"][obs_name]
        cycles = {}
        for code in WEATHER_PART_CODES:
            cr = db.get(CycleResult, (s.id, code))
            cycles[code] = {"base_years": round(cr.base_years, 2), "lo_years": round(cr.lo_years, 2), "hi_years": round(cr.hi_years, 2), "judgment": cr.judgment}
        stations.append({"id": s.id, "name": s.name, "observatory": obs_name, "weather": obs_climate, "cycles": cycles})
    return {"stations": stations, "reference": climate["ref"]}


@app.get("/api/weather-impact/simulate")
def simulate_weather(station_id: int, d_temp: float = 0.0, d_rh: float = 0.0, d_dt: float = 0.0, db: Session = Depends(get_db)):
    """한 역의 10년 날씨를 기온 d_temp·습도 d_rh·일교차 d_dt만큼 통째로 바꿔서 다시 계산한다.

    실제 저장된 계산(기간 평균 가속 계수)과 같은 방식을 써야 "지금 날씨" 결과와 비교할 수 있다.
    """
    station = db.get(Station, station_id)
    if station is None:
        raise HTTPException(404, "역 없음")
    af = _shifted_period_af(station.observatory_id, d_temp, d_rh, d_dt)
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

    parts = db.execute(select(Part)).scalars().all()
    for part in parts:
        cycle = _part_cycle(part, db)
        due = next_due_date(part.install_date, cycle.base_years)
        days_left = (due - today).days
        status = judge_status(days_left, part.has_record)
        if status == "확인_필요":
            check[part.part_type_code] += 1
            continue
        d = today if days_left < 0 else due
        cycle_days = round(cycle.base_years * 365.25)
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


@app.post("/api/replacement-records", status_code=201)
def create_replacement_record(payload: ReplacementRecordIn, db: Session = Depends(get_db)):
    station = db.get(Station, payload.station_id)
    if station is None:
        raise HTTPException(404, "역 없음")

    door = None
    if payload.door_id is not None:
        door = db.get(Door, payload.door_id)
        if door is None or door.station_id != payload.station_id:
            raise HTTPException(404, "그 역에 그 문이 없음")

    parts = {
        p.part_type_code: p
        for p in db.execute(
            select(Part).where(
                Part.station_id == payload.station_id,
                Part.door_id == payload.door_id,
                Part.part_type_code.in_(payload.part_type_codes),
            )
        ).scalars()
    }
    missing = set(payload.part_type_codes) - set(parts)
    if missing:
        raise HTTPException(400, f"그 역·문에 없는 부품 종류: {sorted(missing)}")

    record = ReplacementRecord(
        replaced_on=payload.replaced_on,
        station_id=payload.station_id,
        door_id=payload.door_id,
        reason=payload.reason,
        created_by=payload.created_by,
    )
    db.add(record)
    db.flush()

    for code in payload.part_type_codes:
        db.add(ReplacementPart(record_id=record.id, part_type_code=code))
        part = parts[code]
        if not part.has_record or payload.replaced_on > part.install_date:
            part.install_date = payload.replaced_on
        part.has_record = True

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


# ---------- 화면 (설계_v2.md 6-3, MVP_계획.md 5단계) ----------
# 목업(목업/동해선_PSD_교체주기_목업.html)은 그대로 두고, 실제 API로 연결한 화면만 여기서 띄운다.

WEB_DIR = Path(__file__).resolve().parent / "web"
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
