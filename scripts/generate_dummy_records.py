"""정비기록 더미데이터 v2 — 부품마다 일생을 따라가며 고장·수리·교체·점검을 만든다 (2026-11-16, 데이터셋_v2_설계.md 2장).

예전 방식(2026-10-25)은 날씨 공식 주기를 평균으로 부품마다 고장 날짜 하나를 뽑았다. 그러면 보정 모델이 공식 값을
다시 찾을 뿐이라 "보정이 맞는지" 보여 줄 수 없었다(향후_방향_MVP이후.md 6-1의 3번). 그래서:

1. **숨은 정답**을 공식과 일부러 다르게 둔다 — 부품별 진짜 형상모수(β), 공식 대비 치우침, 역별 흔들림, 문별 약함.
   정답은 data/dummy/정답_수명모수.json에 따로 저장하고 앱은 읽지 않는다(검증·시뮬레이션 전용).
2. **날씨에 따른 계절 변화** — 날씨에 민감한 부품(모터·DCM·IFM)은 그 역 AWS 지점의 일별 가속 계수(psd/cycle.py
   daily_af)를 기간 평균으로 나눈 만큼 그날 나이를 더 먹는다. 습한 여름·해안 역에 고장이 몰린다.
3. **조치와 가상 나이**(Kijima 1형) — 고장 나면 부품별 확률로 조치를 고르고, 조치에 따라 나이가 줄어든다:
   전체 교체 0, 일부 교체 ×0.4, 전체 수리 ×0.3, 일부 수리 그대로(고장 직전과 같음). 다음 고장은 지금 가상 나이에서
   조건부 와이블로 뽑는다.
4. **다른 사건** — 함께 정비(고장 교체 때 같은 문의 짝 부품을 같이 교체), 2024년부터 반기 정기 점검(이상 없음 /
   반년 안에 고장 날 부품을 일정 확률로 찾아 예방 정비).

오늘까지 만든 기록을 두 파일로 나눈다: 기준일(기본 오늘 두 달 전)까지는 초기 데이터, 그 뒤는 시연에서 올리는 샘플
(현장이 아직 안 올린 최근 두 달치). 둘 다 psd/excel_io.py 양식이라 그대로 올릴 수 있다.

    python scripts/generate_dummy_records.py                 # 미리보기(건수만, 저장 안 함)
    python scripts/generate_dummy_records.py --save          # data/dummy/에 세 파일 저장
    python scripts/generate_dummy_records.py --save --seed 7 --today 2026-11-16 --cutoff 2026-09-16

무거운 작업이 아니다(부품 4,400개 × 5년, 몇 초).
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from psd.catalog import DOOR_PART_CODES, SIDES, SYMPTOMS  # noqa: E402
from psd.excel_io import ALL_PARTS, CODE_TO_NAME, REASON_CODE_TO_LABEL, detail_labels, parse_upload, rows_to_excel_bytes  # noqa: E402

DUMMY_DIR = ROOT / "data" / "dummy"
ANSWER_KEY = DUMMY_DIR / "정답_수명모수.json"

# ---------- 1. 숨은 정답 ----------
# 진짜 형상모수: 기계 부품은 마모(β 큼), 전자 부품은 우연 고장에 가까움(β 작음). 보정 쪽은 2로 고정해 두었다(psd/calibrate.py)
TRUE_SHAPE = {"BELT": 3.0, "PULLEY": 2.5, "HANGER": 2.5, "MOTOR": 2.0, "DCM": 1.6, "PSC": 1.5, "IFM": 1.3}
# 진짜 평균 수명 = 날씨 공식 주기 × 이 값. 임의로 정한 "공식이 틀렸다"는 가정 — 보정이 이쪽으로 옮겨 가는지 보려고
TRUE_BIAS = {"MOTOR": 0.9, "BELT": 0.8, "PULLEY": 1.05, "HANGER": 0.9, "DCM": 0.75, "IFM": 1.1, "PSC": 1.0}
STATION_SIGMA = 0.15  # 날씨로 설명 안 되는 역 차이(로그정규 σ)
FRAILTY_VAR = 0.3  # 문별 약함(감마, 평균 1) — 고장 위험에 곱한다
WEAK_DOOR_SHARE = 0.02  # 설치·정렬 문제가 있는 문의 비율 → 반복 고장(psd/repeat.py)
WEAK_DOOR_FACTOR = 3.0

# ---------- 2. 날씨 ----------
# 일별 가속 계수를 원인 비중(psd/cycle.py 기본값)으로 합친다. 나머지 비중은 날씨 무관(1)
WEATHER_WEIGHTS = {"DCM": {"af_cap": 0.25, "af_sol": 0.25, "af_hum": 0.25}, "IFM": {"af_hum": 0.5}, "MOTOR": {"af_mot": 0.3}}

# ---------- 3. 조치 ----------
AGE_FACTOR = {"full": 0.0, "partial": 0.4, "overhaul": 0.3, "repair": 1.0}  # 조치 뒤 가상 나이 = 조치 전 나이 × 이 값
# 고장 때 조치: (조치, 확률, [(세부 부품, 가중치)]). 일부 교체는 세부 부품이 꼭 있어야 한다(psd/excel_io.py)
FAIL_ACTIONS = {
    "MOTOR": [("full", 0.5, []), ("partial", 0.25, [("MOT_BRG", 5), ("MOT_ENC", 3), ("MOT_WIRE", 2)]), ("overhaul", 0.15, []),
              ("repair", 0.10, [("MOT_WIRE", 1)])],
    "BELT": [("full", 0.7, []), ("repair", 0.3, [])],  # 일부 수리 = 장력 조정(세부 부품 없음)
    "PULLEY": [("full", 0.55, []), ("partial", 0.3, [("PUL_BRG", 1)]), ("repair", 0.15, [])],  # 수리 = 정렬 조정
    "HANGER": [("full", 0.4, []), ("partial", 0.4, [("HNG_ROLL", 7), ("HNG_SHOE", 3)]), ("repair", 0.2, [("HNG_ROLL", 1)])],
    "DCM": [("full", 0.45, []), ("partial", 0.35, [("DCM_CAP", 5), ("DCM_BRD", 3), ("DCM_CONN", 2)]), ("repair", 0.2, [("DCM_CONN", 1)])],
    "IFM": [("full", 0.7, []), ("repair", 0.3, [("IFM_CONT", 1)])],  # 수리 = 접점 청소
    "PSC": [("full", 0.5, []), ("partial", 0.35, [("PSC_MOD", 8), ("PSC_TERM", 2)]), ("repair", 0.15, [("PSC_TERM", 1)])],
}
# 점검에서 상태가 나빠 미리 손볼 때(예방 정비): 기계 부품은 조정·부분 교체도 하고, 전자 부품은 통째로 바꾼다
PREV_ACTIONS = {
    "MOTOR": [("full", 0.6, []), ("partial", 0.4, [("MOT_BRG", 1)])],
    "BELT": [("full", 0.6, []), ("repair", 0.4, [])],
    "PULLEY": [("full", 0.5, []), ("partial", 0.5, [("PUL_BRG", 1)])],
    "HANGER": [("partial", 0.6, [("HNG_ROLL", 1)]), ("full", 0.4, [])],
    "DCM": [("full", 1.0, [])],
    "IFM": [("full", 1.0, [])],
    "PSC": [("full", 0.6, []), ("partial", 0.4, [("PSC_MOD", 1)])],
}
WITH_PROB = 0.15  # 고장으로 전체 교체할 때 같은 문의 짝 부품도 같이 바꿀 확률
PARTNERS = {"MOTOR": ["BELT", "PULLEY"], "BELT": ["PULLEY", "MOTOR"], "PULLEY": ["BELT"], "HANGER": ["PULLEY"], "DCM": ["IFM"], "IFM": ["DCM"]}

# ---------- 4. 점검 ----------
INSPECT_FROM = date(2024, 1, 1)  # 점검 기록을 남기기 시작한 때(가정)
DETECT_HORIZON = 0.5  # 점검에서 "반년(유효 나이) 안에 고장 날" 부품을
DETECT_PROB = 0.5  # 이 확률로 찾아낸다

# ---------- 증상·원인 ----------
# 원인 기본 가중치. 열은 그 역의 열 관련 가속 계수, 습기·부식은 습도 가속 계수만큼 더 무겁게 한다
CAUSE_WEIGHTS = {
    "MOTOR": {"WEAR": 4, "HEAT": 3, "DUST": 1, "SHOCK": 0.5},
    "BELT": {"WEAR": 6, "DUST": 1, "SHOCK": 1},
    "PULLEY": {"WEAR": 5, "DUST": 2, "SHOCK": 1},
    "HANGER": {"WEAR": 5, "DUST": 2, "SHOCK": 2},
    "DCM": {"HEAT": 4, "HUMID": 3, "DUST": 1, "SHOCK": 0.5},
    "IFM": {"HUMID": 5, "HEAT": 1, "DUST": 2},
    "PSC": {"HEAT": 2, "HUMID": 2, "WEAR": 1, "DUST": 1},
}
UNKNOWN_SHARE = 0.2  # 원인 미상
TEAMS = {True: "정비1팀", False: "정비2팀"}  # 부전~일광(1~15번) / 좌천~(16번~)


def pick(rng: random.Random, weighted: list[tuple]) -> object:
    """[(값, 가중치), ...]에서 하나."""
    return rng.choices([w[0] for w in weighted], [w[1] for w in weighted])[0]


@dataclass
class PartSim:
    """부품 하나의 상태. 나이는 '유효 나이'(날씨로 빨라진 만큼 더한 년 수)로 센다."""

    code: str
    scale: float  # 진짜 와이블 척도(이 문의 약함까지 반영)
    shape: float
    cum: np.ndarray  # cum[i] = 시작일부터 i일째 시작까지의 유효 나이 누적(년)
    v: float = 0.0  # last 시점의 가상 나이
    last: int = 0  # 마지막 사건 날(일 번호)
    next_fail: int | None = None

    def age_at(self, i: int) -> float:
        return self.v + float(self.cum[i] - self.cum[self.last])

    def hazard_cum(self, a: float) -> float:
        return (a / self.scale) ** self.shape

    def resample(self, rng: random.Random) -> None:
        """지금 가상 나이에서 조건부 와이블로 다음 고장 날을 뽑는다. 오늘을 넘으면 None(중도절단)."""
        u = rng.random()
        total = self.scale * (self.hazard_cum(self.v) - math.log(1 - u)) ** (1 / self.shape)
        target = self.cum[self.last] + (total - self.v)
        idx = max(int(np.searchsorted(self.cum, target, side="left")), self.last + 1)
        self.next_fail = idx if idx < len(self.cum) - 1 else None

    def renew(self, i: int, action: str, rng: random.Random) -> None:
        self.v = self.age_at(i) * AGE_FACTOR[action]
        self.last = i
        self.resample(rng)


@dataclass
class Event:
    day: int
    reason: str
    action: str
    parts: list[str]
    subs: tuple[str, ...] = ()
    symptom: str | None = None
    cause: str | None = None


@dataclass
class UnitContext:
    heat: float = 1.0  # 그 역의 열 관련 가속 계수(기간 평균)
    hum: float = 1.0  # 습도 가속 계수
    events: list[Event] = field(default_factory=list)


def _choose_action(rng: random.Random, table: list[tuple]) -> tuple[str, tuple[str, ...]]:
    action = pick(rng, [(a, p) for a, p, _subs in table])
    subs = next(s for a, _p, s in table if a == action)
    return action, ((pick(rng, subs),) if subs else ())


def _symptom(rng: random.Random, code: str) -> str:
    return rng.choice([k for k, v in SYMPTOMS.items() if code in v["parts"]])


def _cause(rng: random.Random, code: str, ctx: UnitContext) -> str:
    if rng.random() < UNKNOWN_SHARE:
        return "UNKNOWN"
    w = dict(CAUSE_WEIGHTS[code])
    if "HEAT" in w:
        w["HEAT"] *= ctx.heat
    if "HUMID" in w:
        w["HUMID"] *= ctx.hum
    return pick(rng, list(w.items()))


def simulate_unit(parts: list[PartSim], inspections: list[int], rng: random.Random, ctx: UnitContext) -> UnitContext:
    """문 하나(또는 역 공통 부품 하나)의 사건을 날짜 순으로 만든다. parts는 시작 상태(last=시작일)로 넘긴다."""
    by_code = {p.code: p for p in parts}
    for p in parts:
        p.resample(rng)
    pending = sorted(i for i in inspections if i > min(p.last for p in parts))
    while True:
        fails = [p.next_fail for p in parts if p.next_fail is not None]
        nf = min(fails) if fails else None
        ni = pending[0] if pending else None
        if nf is None and ni is None:
            break
        if nf is not None and (ni is None or nf <= ni):
            day = nf
            p = next(p for p in parts if p.next_fail == day)
            action, subs = _choose_action(rng, FAIL_ACTIONS[p.code])
            ctx.events.append(Event(day, "fail", action, [p.code], subs, _symptom(rng, p.code), _cause(rng, p.code, ctx)))
            p.renew(day, action, rng)
            if action == "full" and rng.random() < WITH_PROB:
                partners = [by_code[c] for c in PARTNERS.get(p.code, []) if c in by_code and by_code[c].next_fail != day]
                if partners:
                    q = rng.choice(partners)
                    ctx.events.append(Event(day, "with", "full", [q.code]))
                    q.renew(day, "full", rng)
            continue
        # 정기 점검
        day = pending.pop(0)
        touched = {c for e in ctx.events if e.day == day for c in e.parts}  # 같은 날 이미 손본 부품은 점검 줄에서 뺀다
        ok = []
        for p in parts:
            if p.code in touched:
                continue
            a = p.age_at(day)
            risk = 1 - math.exp(-(p.hazard_cum(a + DETECT_HORIZON) - p.hazard_cum(a)))
            if rng.random() < DETECT_PROB * risk:
                action, subs = _choose_action(rng, PREV_ACTIONS[p.code])
                ctx.events.append(Event(day, "prev", action, [p.code], subs))
                p.renew(day, action, rng)
            else:
                ok.append(p.code)
        if ok:
            ctx.events.append(Event(day, "check", "none", ok))
    return ctx


# ---------- 날씨 → 유효 나이 누적 ----------


def weather_multipliers(day0: date, n_days: int) -> tuple[dict[tuple[str, str], np.ndarray], dict[str, dict[str, float]]]:
    """(AWS 지점, 부품) → 날마다 나이를 먹는 배수(기간 평균 1), 그리고 지점별 기간 평균 가속 계수.

    날씨 CSV는 2022~2025년이라 그 밖의 날은 가장 가까운 해의 같은 날 값을 쓴다(2/29는 2/28)."""
    import pandas as pd

    from psd.cycle import daily_af, load_aws_weather, period_average_af, reference_climate

    weather = load_aws_weather()
    ref = reference_climate(weather)
    daily = daily_af(weather, ref)
    years = sorted(daily["date"].dt.year.unique())
    grid = []
    for i in range(n_days):
        d = day0 + timedelta(days=i)
        y = min(max(d.year, years[0]), years[-1])
        grid.append(pd.Timestamp(date(y, d.month, 28 if (d.month, d.day) == (2, 29) else d.day)))
    out = {}
    for stn, g in daily.groupby("stn"):
        g = g.set_index("date")
        for code, weights in WEATHER_WEIGHTS.items():
            combined = sum(g[k] * w for k, w in weights.items()) + (1 - sum(weights.values()))
            m = (combined / combined.mean()).reindex(grid).fillna(1.0).to_numpy()
            out[(stn, code)] = m
    return out, period_average_af(daily)


def cumulative(mult: np.ndarray) -> np.ndarray:
    """cum[i] = 0~i-1일의 배수 합 / 365.25 (길이 n+1)."""
    return np.concatenate([[0.0], np.cumsum(mult) / 365.25])


def inspection_days(order: int, day0: date, today: date, rng: random.Random) -> list[int]:
    """역마다 반기 점검: 역 순서로 달을 엇갈려(현장이 한 달에 몇 역씩 도는 것처럼) 그 달 5~25일 중 하루."""
    m0 = order % 6 + 1
    days = []
    for y in range(INSPECT_FROM.year, today.year + 1):
        for m in (m0, m0 + 6):
            d = date(y, m, rng.randint(5, 25))
            if INSPECT_FROM <= d <= today:
                days.append((d - day0).days)
    return days


# ---------- 전체 ----------


@dataclass(frozen=True)
class DoorInfo:
    id: int
    station_id: int
    side: str
    car: int
    position: int
    label: str


@dataclass
class SimInputs:
    """DB에서 한 번 읽어 두는 것: 운영 중인 역, 역 x 부품 날씨 공식 주기, 문."""
    stations: list
    base: dict[tuple[int, str], float]
    doors: dict[int, list[DoorInfo]]


def load_inputs(session) -> SimInputs:
    from sqlalchemy import select

    from psd.cycle import STATIONS
    from psd.models import CycleResult, Door

    base = {(c.station_id, c.part_type_code): c.base_years for c in session.execute(select(CycleResult)).scalars()}
    doors: dict[int, list[DoorInfo]] = {}
    for d in session.execute(select(Door).order_by(Door.id)).scalars():
        doors.setdefault(d.station_id, []).append(DoorInfo(d.id, d.station_id, d.side, d.car, d.position, d.label))
    return SimInputs([s for s in STATIONS if s.in_service], base, doors)


@dataclass
class SimUnit:
    """문 하나(또는 역 공통 부품 하나)의 시뮬레이션 결과. parts는 진짜 수명 모수(척도·형상·날씨 누적)를 가진다."""
    station: object
    door: DoorInfo | None
    start: int
    parts: list[PartSim]
    events: list[Event]
    frailty: float = 1.0


def simulate_all(inputs: SimInputs, today: date, seed: int, shape: dict | None = None, bias: dict | None = None,
                 weather: tuple | None = None) -> tuple[list[SimUnit], dict, date]:
    """전체 시뮬레이션(DB 없음). shape·bias로 정답 설정을 바꿀 수 있다(가상 현장 시나리오).
    weather = weather_multipliers 결과(여러 번 돌릴 때 다시 계산하지 않게). 돌려주는 것: (단위들, 정답, 0번 날)."""
    from psd.calibrate import PSD_START

    shape = shape or TRUE_SHAPE
    bias = bias or TRUE_BIAS
    rng = random.Random(seed)
    day0 = min(PSD_START[s.order] for s in inputs.stations)
    n_days = (today - day0).days + 1
    mult, af_by_stn = weather or weather_multipliers(day0, n_days)
    flat = cumulative(np.ones(n_days))
    truth = {"seed": seed, "today": today.isoformat(), "true_shape": shape, "true_bias": bias,
             "station_sigma": STATION_SIGMA, "frailty_var": FRAILTY_VAR, "weak_door_factor": WEAK_DOOR_FACTOR,
             "age_factor": AGE_FACTOR, "station_part": [], "weak_doors": []}
    out: list[SimUnit] = []
    for s in inputs.stations:
        start = (PSD_START[s.order] - day0).days
        af = af_by_stn[s.aws_stn]
        ctx_heat, ctx_hum = (af["cap"] + af["sol"] + af["mot"]) / 3, af["hum"]
        inspections = inspection_days(s.order, day0, today, rng)
        scale0 = {}
        for code in DOOR_PART_CODES + ["PSC"]:
            formula = inputs.base.get((s.order, code), 10.0)
            mean = formula * bias[code] * math.exp(rng.gauss(0, STATION_SIGMA))
            scale0[code] = mean / math.gamma(1 + 1 / shape[code])
            truth["station_part"].append({"station": s.name, "part": code, "formula_years": round(formula, 3),
                                          "true_mean_years": round(mean, 3), "true_shape": shape[code]})

        def cum_of(code: str) -> np.ndarray:
            return cumulative(mult[(s.aws_stn, code)][:n_days]) if code in WEATHER_WEIGHTS else flat

        units = []
        for door in inputs.doors.get(s.order, []):
            z = rng.gammavariate(1 / FRAILTY_VAR, FRAILTY_VAR)
            if rng.random() < WEAK_DOOR_SHARE:
                z *= WEAK_DOOR_FACTOR
                truth["weak_doors"].append({"station": s.name, "door": door.label, "frailty": round(z, 2)})
            parts = [PartSim(c, scale0[c] / z ** (1 / shape[c]), shape[c], cum_of(c), last=start) for c in DOOR_PART_CODES]
            units.append(SimUnit(s, door, start, parts, [], z))
        units.append(SimUnit(s, None, start, [PartSim("PSC", scale0["PSC"], shape["PSC"], cum_of("PSC"), last=start)], []))
        # 역 평균 수명: 이 역 문 전체의 평균(문마다 약함이 달라 "약함 1인 문"의 수명 true_mean_years와 다르다 — β가 작을수록
        # 크게 다름, β 1.2면 약 30% 길다). 교체주기는 그 역 같은 부품 전체에 하나라 보정이 맞혀야 할 값은 이쪽이다(2026-11-16 5단계)
        for row in truth["station_part"][-len(scale0):]:
            code = row["part"]
            lives = [p.scale * math.gamma(1 + 1 / p.shape) for u in units for p in u.parts if p.code == code]
            row["true_station_mean_years"] = round(float(np.mean(lives)), 3)

        for u in units:
            u.events = simulate_unit(u.parts, inspections, rng, UnitContext(heat=ctx_heat, hum=ctx_hum)).events
        out.extend(units)
    return out, truth, day0


def build(session, today: date, seed: int) -> tuple[list[dict], dict]:
    """DB의 역·문·날씨 공식 주기로 전체 기록(엑셀 행)과 정답을 만든다."""
    units, truth, day0 = simulate_all(load_inputs(session), today, seed)
    rows: list[dict] = []
    for u in units:
        s, door = u.station, u.door
        all_codes = DOOR_PART_CODES if door else ["PSC"]
        for e in u.events:
            part_text = ALL_PARTS if e.reason == "check" and sorted(e.parts) == sorted(all_codes) else \
                ",".join(CODE_TO_NAME[c] for c in sorted(e.parts, key=all_codes.index))
            rows.append({
                "역": s.name, "방향": SIDES[door.side] if door else "", "문번호": f"{door.car}-{door.position}" if door else "",
                "작업일": (day0 + timedelta(days=e.day)).isoformat(), "사유": REASON_CODE_TO_LABEL[e.reason],
                "부품": part_text, **detail_labels(e.action, e.subs, e.symptom, e.cause), "입력자": TEAMS[s.order <= 15],
            })
    rows.sort(key=lambda r: (r["작업일"], r["역"], r["방향"], r["문번호"]))
    return rows, truth


def summary(rows: list[dict]) -> str:
    reasons = Counter(r["사유"] for r in rows)
    actions = Counter(r["조치"] for r in rows if r["사유"] != REASON_CODE_TO_LABEL["check"])
    fails = Counter(r["부품"] for r in rows if r["사유"] == "고장")
    return (f"{len(rows):,}줄 · " + " · ".join(f"{k} {v:,}" for k, v in reasons.most_common()) + "\n"
            "조치(점검 제외): " + " · ".join(f"{k} {v:,}" for k, v in actions.most_common()) + "\n"
            "고장 부품: " + " · ".join(f"{k} {v:,}" for k, v in fails.most_common()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true", help="data/dummy/에 초기 데이터·샘플·정답 파일을 저장")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--today", help="기록을 만들 마지막 날(YYYY-MM-DD). 기본 오늘")
    ap.add_argument("--cutoff", help="초기 데이터의 마지막 날. 그 뒤는 샘플. 기본 오늘 두 달 전")
    args = ap.parse_args()

    from psd.db import get_session

    today = date.fromisoformat(args.today) if args.today else date.today()
    if args.cutoff:
        cutoff = date.fromisoformat(args.cutoff)
    else:  # 두 달 전 같은 날(28일 넘으면 28일)
        y, m = divmod(today.year * 12 + today.month - 1 - 2, 12)
        cutoff = date(y, m + 1, min(today.day, 28))
    session = get_session()
    try:
        rows, truth = build(session, today, args.seed)
    finally:
        session.close()
    initial = [r for r in rows if r["작업일"] <= cutoff.isoformat()]
    sample = [r for r in rows if r["작업일"] > cutoff.isoformat()]
    truth["cutoff"] = cutoff.isoformat()
    print(f"기준 {today}, 초기 데이터 ~{cutoff} / 샘플 {cutoff + timedelta(days=1)}~")
    print("[전체] " + summary(rows))
    print("[샘플] " + summary(sample))
    print(f"약한 문 {len(truth['weak_doors'])}개")
    if not args.save:
        print("--save 없이 실행해서 저장은 안 함")
        return

    DUMMY_DIR.mkdir(parents=True, exist_ok=True)
    for name, part in ((f"초기데이터_정비기록_{len(initial)}건.xlsx", initial), (f"정비기록_샘플_{len(sample)}건.xlsx", sample)):
        content = rows_to_excel_bytes(part)
        parse_upload(content)  # 업로드 화면과 같은 규칙으로 읽히는지 확인(틀리면 여기서 에러)
        (DUMMY_DIR / name).write_bytes(content)
        print(f"저장: data/dummy/{name}")
    ANSWER_KEY.write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"저장: data/dummy/{ANSWER_KEY.name} (앱은 읽지 않음 — 검증용 정답)")


if __name__ == "__main__":
    main()
