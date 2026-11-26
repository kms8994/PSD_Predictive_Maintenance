"""정비기록 더미데이터 v3 — 장애 이력 양식 모양, 소분류마다 일생을 따라가며 사건을 만든다 (2026-11-24, 데이터셋_v3_설계.md 7장).

v2(2026-11-16)의 방식을 그대로 두고 단위만 소분류로 바꿨다:

1. **숨은 정답**을 소분류마다 둔다 — 형상모수(β), 평균 수명, 날씨 민감도, 역별 흔들림, 문별 약함.
   - 날씨 공식이 있는 소분류: 평균 수명 = 공식 주기 × 치우침(공식이 일부러 틀리게).
   - 공식이 없는 소분류: 평균 수명을 4~15년에서 뽑는다. 이 중 일부는 일부러 습도·기온에 민감하게 둔다 —
     AI가 기록만으로 그걸 찾아내는지 보려고.
   정답은 정답 파일에 따로 저장하고 앱은 읽지 않는다. 정답은 --truth-seed로 정해져 가상 노선을 여러 벌 만들어도 같다
   (같은 동해선을 여러 번 겪는 셈 — 사건만 --seed마다 다르다).
2. **날씨에 따른 계절 변화** — 날씨에 민감한 소분류는 그 역 AWS 지점의 일별 가속 계수(psd/cycle.py daily_af)만큼 나이를 더 먹는다.
3. **조치와 가상 나이**(Kijima 1형) — 고장(사후조치) 나면 조치를 고르고 나이를 줄인다: 교체 0, 단순교체 ×0.4,
   수리·조정·리셋·기타는 그대로(고장 직전과 같음). 보정(psd/calibrate.py)은 단순교체를 0.5로 가정한다 — 일부러 다르게 뒀다.
4. **사전조치** — 2024년부터 반기 정기 점검에서 반년 안에 고장 날 부품을 일정 확률로 찾아 교체·단순교체·조정한다.
   양식에 '점검(이상 없음)'이 없어 이상 없는 점검은 기록하지 않는다.

출력은 코레일 양식과 같은 칸의 엑셀(psd/excel_io.py). 기준일(기본 오늘 두 달 전)까지는 초기 데이터, 그 뒤는 시연에서 올리는 샘플.
분류표가 코레일 자료라 결과도 data/private/dummy/(.gitignore)에 둔다.

    python scripts/generate_dummy_records.py                 # 미리보기(건수만, 저장 안 함)
    python scripts/generate_dummy_records.py --save          # data/private/dummy/에 세 파일 저장
    python scripts/generate_dummy_records.py --save --no-db  # DB 없이(데스크탑 등) — 문은 상·하 본선만
    python scripts/generate_dummy_records.py --save --seed 7 --today 2026-11-24 --cutoff 2026-09-24

한 벌은 무거운 작업이 아니다(부품 약 1만 개 × 5년, 수십 초). 여러 벌로 학습하는 건 scripts/pretrain_risk_model.py(데스크탑).
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from psd.catalog import CARS_PER_SIDE, DOORS_PER_SIDE, MAIN_SIDES, PART_TYPES, REASONS, SIDES, TAXONOMY  # noqa: E402
from psd.excel_io import parse_upload, record_row, rows_to_excel_bytes  # noqa: E402

DUMMY_DIR = ROOT / "data" / "private" / "dummy"
ANSWER_KEY = DUMMY_DIR / "정답_수명모수.json"
UNITS = {p["code"]: p for p in PART_TYPES}

# ---------- 1. 숨은 정답 ----------
# 공식이 있는 소분류(코드는 분류표와 예시 분류표에 함께 있는 것): v2 부품의 정답을 이어받는다
KNOWN_SHAPE = {"BELT": 3.0, "HANGER": 2.5, "MOTOR": 2.0, "DCU": 1.6, "PSC": 1.5}
KNOWN_BIAS = {"MOTOR": 0.9, "BELT": 0.8, "HANGER": 0.9, "DCU": 0.75, "PSC": 1.0}
UNKNOWN_LIFE = (4.0, 15.0)  # 공식 없는 소분류의 평균 수명 범위(년, 로그 균등)
HIDDEN_WEATHER_SHARE = 0.35  # 공식 없는 소분류 중 몰래 날씨에 민감한 비율
STATION_SIGMA = 0.15  # 날씨로 설명 안 되는 역 차이(로그정규 σ)
FRAILTY_VAR = 0.3  # 문별 약함(감마, 평균 1) — 고장 위험에 곱한다
WEAK_DOOR_SHARE = 0.02  # 설치·정렬 문제가 있는 문의 비율 → 반복 고장(psd/repeat.py)
WEAK_DOOR_FACTOR = 3.0

# ---------- 2. 날씨 ----------
# 공식 소분류는 psd/cycle.py 기본 비중과 같게. 나머지 비중은 날씨 무관(1)
FORMULA_WEATHER = {"dcm": {"af_cap": 0.25, "af_sol": 0.25, "af_hum": 0.25}, "motor": {"af_mot": 0.3}, "humid": {"af_hum": 0.5}}

# ---------- 3. 조치 ----------
AGE_FACTOR = {"full": 0.0, "partial": 0.4, "repair": 1.0, "adjust": 1.0, "reset": 1.0, "other": 1.0}
# 사후조치 때 조치 확률: 대분류 성격으로 나눈다(기계 / 센서 / 전자)
FAIL_ACTIONS = {
    "mech": [("full", 0.35), ("partial", 0.25), ("repair", 0.15), ("adjust", 0.15), ("reset", 0.05), ("other", 0.05)],
    "sensor": [("full", 0.35), ("adjust", 0.25), ("other", 0.2), ("reset", 0.1), ("repair", 0.05), ("partial", 0.05)],
    "elec": [("full", 0.45), ("reset", 0.25), ("repair", 0.1), ("partial", 0.1), ("adjust", 0.05), ("other", 0.05)],
}
PREV_ACTIONS = [("full", 0.6), ("partial", 0.2), ("adjust", 0.2)]

# ---------- 4. 점검 ----------
INSPECT_FROM = date(2024, 1, 1)  # 사전조치 기록을 남기기 시작한 때(가정)
DETECT_HORIZON = 0.5  # 점검에서 "반년(유효 나이) 안에 고장 날" 부품을
DETECT_PROB = 0.5  # 이 확률로 찾아낸다

# ---------- 글 칸 ----------
CAUSES = [("마모", 4), ("노후 열화", 3), ("습기로 인한 부식", 2), ("이물질 유입", 2), ("외부 충격", 1), ("원인 불명", 2)]
RECEIVERS = ["김철도", "홍레일", "박선로", "이역무"]  # 지어낸 이름
TEAMS = {True: "정비1팀", False: "정비2팀"}  # 부전~일광(1~15번) / 좌천~(16번~)
STATION_INFO = {"hq": "부산경남", "line_name": "동해선", "office": "부전건축사업소"}


def kind_of(code: str) -> str:
    group = UNITS[code]["group"]
    if "센서" in group:
        return "sensor"
    if any(k in group for k in ("구동부", "도어", "기타")):
        return "mech"
    return "elec"


def pick(rng: random.Random, weighted: list[tuple]) -> object:
    """[(값, 가중치), ...]에서 하나."""
    return rng.choices([w[0] for w in weighted], [w[1] for w in weighted])[0]


def make_truth(truth_seed: int) -> dict[str, dict]:
    """소분류마다 숨은 정답: shape, bias(공식 대비) 또는 mean_years(공식 없음), weather(일별 가속 계수 비중)."""
    out = {}
    for p in PART_TYPES:
        code = p["code"]
        rng = random.Random(f"{truth_seed}-{code}")
        if p["formula"] is not None:
            out[code] = {"shape": KNOWN_SHAPE.get(code, round(rng.uniform(1.3, 2.8), 2)),
                         "bias": KNOWN_BIAS.get(code, round(math.exp(rng.gauss(0, 0.2)), 3)),
                         "weather": FORMULA_WEATHER.get(p["formula"], {})}
        else:
            weather = {}
            if rng.random() < HIDDEN_WEATHER_SHARE:
                weather = {rng.choice(["af_hum", "af_cap"]): round(rng.uniform(0.3, 0.6), 2)}
            lo, hi = UNKNOWN_LIFE
            out[code] = {"shape": round(rng.uniform(1.1, 3.0), 2),
                         "mean_years": round(math.exp(rng.uniform(math.log(lo), math.log(hi))), 2), "weather": weather}
    return out


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
        """지금 가상 나이에서 조건부 와이블로 다음 고장 날을 뽑는다. 끝날을 넘으면 None(중도절단)."""
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
    reason: str  # fail | prev
    action: str
    parts: list[str]  # 소분류 코드 하나


def simulate_unit(parts: list[PartSim], inspections: list[int], rng: random.Random) -> list[Event]:
    """문 하나(또는 역 단위 부품 하나)의 사건을 날짜 순으로 만든다. parts는 시작 상태(last=시작일)로 넘긴다."""
    events: list[Event] = []
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
            p = next(p for p in parts if p.next_fail == nf)
            action = pick(rng, FAIL_ACTIONS[kind_of(p.code)])
            events.append(Event(nf, "fail", action, [p.code]))
            p.renew(nf, action, rng)
            continue
        day = pending.pop(0)  # 정기 점검: 반년 안에 고장 날 부품을 일정 확률로 찾아 손본다
        for p in parts:
            a = p.age_at(day)
            risk = 1 - math.exp(-(p.hazard_cum(a + DETECT_HORIZON) - p.hazard_cum(a)))
            if rng.random() < DETECT_PROB * risk:
                action = pick(rng, PREV_ACTIONS)
                events.append(Event(day, "prev", action, [p.code]))
                p.renew(day, action, rng)
    return events


# ---------- 날씨 → 유효 나이 누적 ----------


def weather_multipliers(day0: date, n_days: int, truth: dict[str, dict]) -> tuple[dict[tuple[str, str], np.ndarray], dict[str, dict[str, float]]]:
    """(AWS 지점, 소분류) → 날마다 나이를 먹는 배수(기간 평균 1), 그리고 지점별 기간 평균 가속 계수.
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
        for code, t in truth.items():
            if not t["weather"]:
                continue
            combined = sum(g[k] * w for k, w in t["weather"].items()) + (1 - sum(t["weather"].values()))
            out[(stn, code)] = (combined / combined.mean()).reindex(grid).fillna(1.0).to_numpy()
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


# ---------- 입력: 역·문·공식 주기 ----------


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
    """운영 중인 역, 역 x 소분류 날씨 공식 주기, 문."""
    stations: list
    base: dict[tuple[int, str], float]
    doors: dict[int, list[DoorInfo]]


def load_inputs(session) -> SimInputs:
    """DB의 역·문(기록으로 생긴 부본선 포함)·계산 결과로."""
    from sqlalchemy import select

    from psd.cycle import STATIONS
    from psd.models import CycleResult, Door

    base = {(c.station_id, c.part_type_code): c.base_years for c in session.execute(select(CycleResult)).scalars()}
    doors: dict[int, list[DoorInfo]] = {}
    for d in session.execute(select(Door).order_by(Door.id)).scalars():
        doors.setdefault(d.station_id, []).append(DoorInfo(d.id, d.station_id, d.side, d.car, d.position, d.label))
    return SimInputs([s for s in STATIONS if s.in_service], base, doors)


def inputs_without_db() -> SimInputs:
    """DB 없이(데스크탑 학습용): 공식 주기는 psd/cycle.py로 바로 계산하고, 문은 상·하 본선만 scripts/init_db.py와 같은 순서로."""
    from psd.cycle import STATIONS, compute_all

    order = {s.name: s.id for s in STATIONS}
    base = {(order[r.station], r.part): r.cycle.base_years for r in compute_all()}
    doors: dict[int, list[DoorInfo]] = {}
    did = 0
    for s in STATIONS:
        if not s.in_service:
            continue
        for side in MAIN_SIDES:
            for n in range(1, DOORS_PER_SIDE + 1):
                did += 1
                car, pos = (n - 1) // CARS_PER_SIDE + 1, (n - 1) % CARS_PER_SIDE + 1
                doors.setdefault(s.id, []).append(DoorInfo(did, s.id, side, car, pos, f"{SIDES[side]} {car}-{pos}"))
    return SimInputs([s for s in STATIONS if s.in_service], base, doors)


@dataclass
class SimUnit:
    """문 하나(또는 역 단위 부품 하나)의 시뮬레이션 결과. parts는 진짜 수명 모수(척도·형상·날씨 누적)를 가진다."""
    station: object
    door: DoorInfo | None
    start: int
    parts: list[PartSim]
    events: list[Event]
    frailty: float = 1.0


def simulate_all(inputs: SimInputs, today: date, seed: int, truth_seed: int = 2026, truth: dict | None = None,
                 weather: tuple | None = None) -> tuple[list[SimUnit], dict, date]:
    """전체 시뮬레이션(DB 없음). truth로 정답 설정을 바꿀 수 있다(가상 현장 시나리오). weather = weather_multipliers 결과
    (여러 번 돌릴 때 다시 계산하지 않게). 돌려주는 것: (단위들, 정답 기록, 0번 날)."""
    from psd.calibrate import PSD_START

    truth = truth or make_truth(truth_seed)
    trng = random.Random(f"{truth_seed}-stations")  # 역별 흔들림도 정답이라 사건 씨앗과 따로
    rng = random.Random(seed)
    day0 = min(PSD_START[s.id] for s in inputs.stations)
    n_days = (today - day0).days + 1
    mult, af_by_stn = weather or weather_multipliers(day0, n_days, truth)
    flat = cumulative(np.ones(n_days))
    door_codes = [p["code"] for p in PART_TYPES if p["unit"] == "door"]
    station_codes = [p["code"] for p in PART_TYPES if p["unit"] == "station"]
    record = {"seed": seed, "truth_seed": truth_seed, "today": today.isoformat(), "taxonomy": TAXONOMY.source,
              "units": truth, "station_sigma": STATION_SIGMA, "frailty_var": FRAILTY_VAR, "weak_door_factor": WEAK_DOOR_FACTOR,
              "age_factor": AGE_FACTOR, "station_part": [], "weak_doors": []}
    out: list[SimUnit] = []
    for s in inputs.stations:
        start = (PSD_START[s.id] - day0).days
        inspections = inspection_days(s.seq, day0, today, rng)
        scale0 = {}
        for code, t in truth.items():
            formula = inputs.base.get((s.id, code), 10.0)
            mean = (formula * t["bias"] if "bias" in t else t["mean_years"]) * math.exp(trng.gauss(0, STATION_SIGMA))
            scale0[code] = mean / math.gamma(1 + 1 / t["shape"])
            record["station_part"].append({"station": s.name, "part": code, "formula_years": round(formula, 3),
                                           "true_mean_years": round(mean, 3), "true_shape": t["shape"]})

        def cum_of(code: str) -> np.ndarray:
            return cumulative(mult[(s.aws_stn, code)][:n_days]) if (s.aws_stn, code) in mult else flat

        units = []
        for door in inputs.doors.get(s.id, []):
            z = rng.gammavariate(1 / FRAILTY_VAR, FRAILTY_VAR)
            if rng.random() < WEAK_DOOR_SHARE:
                z *= WEAK_DOOR_FACTOR
                record["weak_doors"].append({"station": s.name, "door": door.label, "frailty": round(z, 2)})
            parts = [PartSim(c, scale0[c] / z ** (1 / truth[c]["shape"]), truth[c]["shape"], cum_of(c), last=start) for c in door_codes]
            units.append(SimUnit(s, door, start, parts, [], z))
        for c in station_codes:
            units.append(SimUnit(s, None, start, [PartSim(c, scale0[c], truth[c]["shape"], cum_of(c), last=start)], []))
        # 역 평균 수명: 이 역 문 전체의 평균(문마다 약함이 달라 "약함 1인 문"의 수명과 다르다). 보정이 맞혀야 할 값은 이쪽이다
        for row in record["station_part"][-len(truth):]:
            lives = [p.scale * math.gamma(1 + 1 / p.shape) for u in units for p in u.parts if p.code == row["part"]]
            row["true_station_mean_years"] = round(float(np.mean(lives)), 3) if lives else row["true_mean_years"]
        for u in units:
            u.events = simulate_unit(u.parts, inspections, rng)
        out.extend(units)
    return out, record, day0


# ---------- 양식 줄로 ----------


def _clock(rng: random.Random, d: date, hours_after: float = 0.0) -> tuple[str, str]:
    h, m = rng.randint(5, 23), rng.choice([0, 10, 20, 30, 40, 50])
    end = h + m / 60 + hours_after
    eh, em = int(end) % 24, int(round((end % 1) * 60)) % 60
    return f"{d.month}월 {d.day}일 {h:02d}:{m:02d}", f"{d.month}월 {d.day}일 {eh:02d}:{em:02d}"


def _texts(rng: random.Random, code: str, e: Event, d: date, hum: float) -> dict:
    u = UNITS[code]
    unit_name = u["name"].split("(")[0]
    symptom = rng.choice(u["symptoms"]) if u["symptoms"] else ""
    tax = TAXONOMY.by_code(code)
    main = rng.choice(tax.main_parts) if tax.main_parts else unit_name
    minor = rng.choice(tax.minor_parts) if tax.minor_parts else main
    detail = {"full": f"{main} 교체", "partial": f"{minor} 교체", "repair": f"{main} 수리", "adjust": f"{unit_name} 위치·간격 조정",
              "reset": rng.choice(["전원 리셋", "설정값 재설정", "커넥터 재결선"]), "other": rng.choice(["이물질 제거", "청소 후 정상 확인"])}[e.action]
    out = {"symptom": symptom or None, "symptom_detail": f"{unit_name} {symptom}".strip(), "action_detail": detail}
    if e.reason == "fail":
        causes = [(c, w * (hum if "습기" in c else 1.0)) for c, w in CAUSES]
        received, completed = _clock(rng, d, rng.uniform(0.3, 6.0))
        out.update(cause_text=f"{unit_name} {pick(rng, causes)}", receiver=rng.choice(RECEIVERS), received=received, completed=completed)
    else:
        out.update(cause_text="정기 점검 중 열화 발견", receiver=None, received=None, completed=None)
    return out


def to_rows(units: list[SimUnit], day0: date, seed: int) -> list[dict]:
    """시뮬레이션 결과 → 양식 줄(psd/excel_io.record_row)."""
    from psd.calibrate import PSD_START

    rng = random.Random(seed + 1)
    rows: list[dict] = []
    for u in units:
        s, door = u.station, u.door
        info = {**STATION_INFO, "install_year": PSD_START[s.id].year}
        for e in u.events:
            d = day0 + timedelta(days=e.day)
            code = e.parts[0]
            rows.append(record_row(
                day=d, station=s.name, reason=e.reason, unit_code=code, action=e.action,
                side=door.side if door else None, door_no=f"{door.car}-{door.position}" if door else None,
                worker=TEAMS[s.seq <= 15], station_info=info, **_texts(rng, code, e, d, 1.0 + 0.5 * s.near_water),
            ))
    rows.sort(key=lambda r: (r["년도"], r["월"], r["일"], r["역"], r["승강장 선로"], r["출입문"], r["장애부위②(소분류)"]))
    return rows


def summary(rows: list[dict]) -> str:
    reasons = Counter(r["장애인지"] for r in rows)
    actions = Counter(r["조치사항"] for r in rows)
    fails = Counter(r["장애부위②(소분류)"] for r in rows if r["장애인지"] == REASONS["fail"])
    return (f"{len(rows):,}줄 · " + " · ".join(f"{k} {v:,}" for k, v in reasons.most_common()) + "\n"
            "조치: " + " · ".join(f"{k} {v:,}" for k, v in actions.most_common()) + "\n"
            "사후조치 많은 소분류: " + " · ".join(f"{k} {v:,}" for k, v in fails.most_common(8)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true", help="data/private/dummy/에 초기 데이터·샘플·정답 파일을 저장")
    ap.add_argument("--seed", type=int, default=2026, help="사건 씨앗")
    ap.add_argument("--truth-seed", type=int, default=2026, help="숨은 정답 씨앗(가상 노선 여러 벌이 같은 정답을 쓰게)")
    ap.add_argument("--today", help="기록을 만들 마지막 날(YYYY-MM-DD). 기본 오늘")
    ap.add_argument("--cutoff", help="초기 데이터의 마지막 날. 그 뒤는 샘플. 기본 오늘 두 달 전")
    ap.add_argument("--no-db", action="store_true", help="DB 없이 만든다(문은 상·하 본선만)")
    args = ap.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    if args.cutoff:
        cutoff = date.fromisoformat(args.cutoff)
    else:  # 두 달 전 같은 날(28일 넘으면 28일)
        y, m = divmod(today.year * 12 + today.month - 1 - 2, 12)
        cutoff = date(y, m + 1, min(today.day, 28))
    if args.no_db:
        inputs = inputs_without_db()
    else:
        from psd.db import get_session

        session = get_session()
        try:
            inputs = load_inputs(session)
        finally:
            session.close()
    units, truth, day0 = simulate_all(inputs, today, args.seed, args.truth_seed)
    rows = to_rows(units, day0, args.seed)
    key = lambda r: date(r["년도"], r["월"], r["일"])  # noqa: E731
    initial = [r for r in rows if key(r) <= cutoff]
    sample = [r for r in rows if key(r) > cutoff]
    truth["cutoff"] = cutoff.isoformat()
    print(f"분류표 {TAXONOMY.source} · 소분류 {len(PART_TYPES)} · 기준 {today}, 초기 데이터 ~{cutoff} / 샘플 {cutoff + timedelta(days=1)}~")
    print("[전체] " + summary(rows))
    print("[샘플] " + summary(sample))
    print(f"약한 문 {len(truth['weak_doors'])}개 · 몰래 날씨에 민감한 소분류 "
          f"{[c for c, t in truth['units'].items() if t['weather'] and UNITS[c]['formula'] is None]}")
    if not args.save:
        print("--save 없이 실행해서 저장은 안 함")
        return

    DUMMY_DIR.mkdir(parents=True, exist_ok=True)
    for old in DUMMY_DIR.glob("*건.xlsx"):
        old.unlink()  # 예전 생성 결과(건수가 파일 이름에 있어 덮어써지지 않는다)
    for name, part in ((f"초기데이터_정비기록_{len(initial)}건.xlsx", initial), (f"정비기록_샘플_{len(sample)}건.xlsx", sample)):
        content = rows_to_excel_bytes(part)
        parse_upload(content)  # 업로드 화면과 같은 규칙으로 읽히는지 확인(틀리면 여기서 에러)
        (DUMMY_DIR / name).write_bytes(content)
        print(f"저장: {(DUMMY_DIR / name).relative_to(ROOT)}")
    ANSWER_KEY.write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"저장: {ANSWER_KEY.relative_to(ROOT)} (앱은 읽지 않음 — 검증용 정답)")


if __name__ == "__main__":
    main()
