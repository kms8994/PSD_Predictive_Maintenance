"""동해선 PSD 부품 교체주기 계산.

근거: 설계_v2.md 4단계, 4단계_계산공식_근거조사.md.
기준 주기(10년)에 역의 날씨로 구한 가속 계수(AF)를 나눠 권장 주기를 낸다.
고장 원인별 비중은 근거가 없으므로 기본값과 함께 범위를 계산해 판정한다.

    python -m psd.cycle          # 24개 역 x 7종 결과 표 출력
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
WEATHER_CSV = ROOT / "data" / "processed" / "weather_daily.csv"

K_EV = 8.617e-5  # 볼츠만 상수 (eV/K)
REPLACEMENT_YEARS = 10.0  # 제조사 권장 주기 (기준 기후에서)


def kelvin(celsius: float) -> float:
    return celsius + 273.15


# ---------- 역 목록 (설계_v2.md "역 목록", obs는 목업과 동일한 가정) ----------


@dataclass(frozen=True)
class Station:
    order: int
    name: str
    open_date: date
    obs: str  # "부산" 또는 "울산" (ASOS 관측소, AWS 승인 전까지 묶음 처리)


_D1 = date(2016, 12, 30)
_D2 = date(2020, 3, 28)
_D3 = date(2021, 12, 28)
_D4 = date(2026, 9, 1)  # 북울산: 개통일 미정, 월초로 임시 처리

STATIONS: list[Station] = [
    Station(1, "부전", _D1, "부산"),
    Station(2, "거제해맞이", _D1, "부산"),
    Station(3, "거제", _D1, "부산"),
    Station(4, "교대", _D1, "부산"),
    Station(5, "동래", _D1, "부산"),
    Station(6, "안락", _D1, "부산"),
    Station(7, "부산원동", _D2, "부산"),
    Station(8, "재송", _D1, "부산"),
    Station(9, "센텀", _D1, "부산"),
    Station(10, "벡스코", _D1, "부산"),
    Station(11, "신해운대", _D1, "부산"),
    Station(12, "송정", _D1, "부산"),
    Station(13, "오시리아", _D1, "부산"),
    Station(14, "기장", _D1, "부산"),
    Station(15, "일광", _D1, "부산"),
    Station(16, "좌천", _D3, "부산"),
    Station(17, "월내", _D3, "부산"),
    Station(18, "서생", _D3, "울산"),
    Station(19, "남창", _D3, "울산"),
    Station(20, "망양", _D3, "울산"),
    Station(21, "덕하", _D3, "울산"),
    Station(22, "개운포", _D3, "울산"),
    Station(23, "태화강", _D3, "울산"),
    Station(24, "북울산", _D4, "울산"),
]

OBS_CODE = {"부산": "159", "울산": "152"}

PARTS = ["모터", "타이밍벨트", "풀리", "도어 행거", "DCM", "IFM 릴레이", "종합제어반 릴레이"]
WEATHER_PARTS = {"DCM", "IFM 릴레이", "모터"}

# 비중 범위 (4단계_계산공식_근거조사.md 3장)
DCM_STEPS = (0.1, 0.2, 0.3, 0.4, 0.5)
DCM_DEFAULT = (0.25, 0.25, 0.25, 0.25)
IFM_WEIGHT_RANGE = (0.2, 0.8, 0.5)  # (최소, 최대, 기본값)
MOTOR_WEIGHT_RANGE = (0.1, 0.5, 0.3)


# ---------- 날씨 ----------


def load_weather() -> pd.DataFrame:
    df = pd.read_csv(WEATHER_CSV, parse_dates=["date"])
    df["stn"] = df["stn"].astype(str)
    return df


def station_counts() -> dict[str, int]:
    counts = {"부산": 0, "울산": 0}
    for s in STATIONS:
        counts[s.obs] += 1
    return counts


def observatory_climate(weather: pd.DataFrame) -> dict[str, dict[str, float]]:
    """관측소별 10년 평균 기후. 부품 상세 화면의 "날씨 근거"에 쓴다."""
    result = {}
    for obs, code in OBS_CODE.items():
        g = weather[weather["stn"] == code]
        result[obs] = {
            "T": g["ta_avg"].mean(),
            "RH": g["hm_avg"].mean(),
            "DT": g["dtr"].mean(),
            "Tmax": g["ta_max"].mean(),
        }
    return result


def reference_climate(weather: pd.DataFrame) -> dict[str, float]:
    """기준 날씨(T_ref, RH_ref, DT_ref, Tmax_ref) = 24개 역 평균 기후.

    역별 실측이 없어 두 관측소의 장기 평균을 역 수로 가중 평균한다.
    """
    counts = station_counts()
    total = sum(counts.values())
    per_obs = {}
    for obs, code in OBS_CODE.items():
        g = weather[weather["stn"] == code]
        per_obs[obs] = {
            "T": g["ta_avg"].mean(),
            "RH": g["hm_avg"].mean(),
            "DT": g["dtr"].mean(),
            "Tmax": g["ta_max"].mean(),
        }
    return {
        key: sum(per_obs[obs][key] * counts[obs] for obs in counts) / total
        for key in ("T", "RH", "DT", "Tmax")
    }


# ---------- 2-1. 원인별 가속 계수 ----------


def af_capacitor(t: float, ref_t: float) -> float:
    """전해 콘덴서 전해액 건조: 10°C마다 수명 절반 (Nichicon·Würth)."""
    return 2 ** ((t - ref_t) / 10)


def af_solder(dt: float, tmax: float, ref_dt: float, ref_tmax: float) -> float:
    """납땜 접합부 열피로: Norris-Landzberg."""
    return (dt / ref_dt) ** 1.9 * math.exp(1414 * (1 / kelvin(ref_tmax) - 1 / kelvin(tmax)))


def af_corrosion(rh: float, t: float, ref_rh: float, ref_t: float) -> float:
    """습도에 의한 금속 부식: Peck 모델 (n=2.7, Ea=0.79 eV)."""
    return (rh / ref_rh) ** 2.7 * math.exp((0.79 / K_EV) * (1 / kelvin(ref_t) - 1 / kelvin(t)))


def af_motor_winding(t: float, ref_t: float) -> float:
    """모터 권선 절연의 열 노화: NSWC-98 식 14-4."""
    return 10 ** (2357 * (1 / kelvin(ref_t) - 1 / kelvin(t)))


# ---------- 2-2. 일별 계수 -> 기간 평균 ----------


def daily_af(weather: pd.DataFrame, ref: dict[str, float]) -> pd.DataFrame:
    """관측소별 일별 가속 계수."""
    out = weather.copy()
    out["af_cap"] = out["ta_avg"].apply(lambda t: af_capacitor(t, ref["T"]))
    out["af_sol"] = [
        af_solder(dt, tmax, ref["DT"], ref["Tmax"]) for dt, tmax in zip(out["dtr"], out["ta_max"])
    ]
    out["af_hum"] = [af_corrosion(rh, t, ref["RH"], ref["T"]) for rh, t in zip(out["hm_avg"], out["ta_avg"])]
    out["af_mot"] = out["ta_avg"].apply(lambda t: af_motor_winding(t, ref["T"]))
    return out


def period_average_af(daily: pd.DataFrame) -> dict[str, dict[str, float]]:
    """관측소별 기간 평균 가속 계수 (AF_m(s) = 일별 AF_m(s, d)의 평균)."""
    result = {}
    for obs, code in OBS_CODE.items():
        g = daily[daily["stn"] == code]
        result[obs] = {
            "cap": g["af_cap"].mean(),
            "sol": g["af_sol"].mean(),
            "hum": g["af_hum"].mean(),
            "mot": g["af_mot"].mean(),
        }
    return result


# ---------- 2-3, 2-4. 비중 합성·범위, 판정 ----------


@dataclass(frozen=True)
class CycleResult:
    base_years: float
    lo_years: float
    hi_years: float
    judgment: str  # "같음" | "짧음_확실" | "김_확실" | "불확실" | "날씨무관"


def judge(lo_years: float, hi_years: float) -> str:
    """4단계_계산공식_근거조사.md 3장 판정 기준. 위에서부터 차례로 본다."""
    if lo_years >= 9.5 and hi_years <= 10.5:
        return "같음"
    if hi_years < 10:
        return "짧음_확실"
    if lo_years > 10:
        return "김_확실"
    return "불확실"


def _years(weighted_af: float) -> float:
    return REPLACEMENT_YEARS / weighted_af


def dcm_cycle(af: dict[str, float]) -> CycleResult:
    """DCM: 콘덴서/납땜/부식/날씨무관 4원인, 10%~50% 격자 전체를 범위로 계산."""
    values = []
    for a, b, c in itertools.product(DCM_STEPS, repeat=3):
        o = 1 - a - b - c
        if 0.1 - 1e-9 <= o <= 0.5 + 1e-9:
            values.append(_years(a * af["cap"] + b * af["sol"] + c * af["hum"] + o))
    a0, b0, c0, o0 = DCM_DEFAULT
    base = _years(a0 * af["cap"] + b0 * af["sol"] + c0 * af["hum"] + o0)
    lo, hi = min(values), max(values)
    return CycleResult(base, lo, hi, judge(lo, hi))


def single_cause_cycle(af_value: float, weight_range: tuple[float, float, float]) -> CycleResult:
    """원인이 하나뿐인 부품(IFM 릴레이, 모터): 비중 구간의 양끝과 기본값만 계산하면 된다."""
    lo_w, hi_w, default_w = weight_range
    years_at = lambda w: _years(w * af_value + (1 - w))
    lo, hi = sorted((years_at(lo_w), years_at(hi_w)))
    base = years_at(default_w)
    return CycleResult(base, lo, hi, judge(lo, hi))


def fixed_cycle() -> CycleResult:
    """날씨와 무관한 부품(타이밍벨트·풀리·도어 행거·종합제어반 릴레이): 10년 유지."""
    return CycleResult(REPLACEMENT_YEARS, REPLACEMENT_YEARS, REPLACEMENT_YEARS, "날씨무관")


def part_cycle(part: str, af: dict[str, float]) -> CycleResult:
    if part == "DCM":
        return dcm_cycle(af)
    if part == "IFM 릴레이":
        return single_cause_cycle(af["hum"], IFM_WEIGHT_RANGE)
    if part == "모터":
        return single_cause_cycle(af["mot"], MOTOR_WEIGHT_RANGE)
    return fixed_cycle()


# ---------- 2-5. 다음 교체 예정일과 경고 상태 ----------


def next_due_date(install_date: date, base_years: float) -> date:
    return install_date + timedelta(days=round(base_years * 365.25))


def warning_status(due: date, today: date, judgment: str) -> str:
    """불확실 판정은 날짜와 무관하게 "확인 필요"로 둔다. 그 외는 남은 기간으로 본다."""
    if judgment == "불확실":
        return "확인_필요"
    days_left = (due - today).days
    if days_left <= 90:
        return "빨강"
    if days_left <= 365:
        return "노랑"
    return "초록"


@dataclass(frozen=True)
class PartStatus:
    station: str
    part: str
    cycle: CycleResult
    next_due: date
    days_left: int
    warning: str


def station_part_status(
    station: Station, part: str, af_by_obs: dict[str, dict[str, float]], today: date
) -> PartStatus:
    cycle = part_cycle(part, af_by_obs[station.obs])
    due = next_due_date(station.open_date, cycle.base_years)
    return PartStatus(station.name, part, cycle, due, (due - today).days, warning_status(due, today, cycle.judgment))


# ---------- 전체 계산 ----------


def compute_all(today: date | None = None) -> list[PartStatus]:
    """24개 역 x 부품 7종의 권장 주기·판정·경고 상태 (2-8)."""
    today = today or date.today()
    weather = load_weather()
    ref = reference_climate(weather)
    af_by_obs = period_average_af(daily_af(weather, ref))
    return [
        station_part_status(station, part, af_by_obs, today)
        for station in STATIONS
        for part in PARTS
    ]


def _print_table() -> None:
    rows = [
        {
            "역": r.station,
            "부품": r.part,
            "주기(년)": round(r.cycle.base_years, 2),
            "범위": f"{r.cycle.lo_years:.1f}-{r.cycle.hi_years:.1f}",
            "판정": r.cycle.judgment,
            "다음 교체": r.next_due,
            "경고": r.warning,
        }
        for r in compute_all()
    ]
    df = pd.DataFrame(rows)
    with pd.option_context("display.max_rows", None, "display.width", 160):
        print(df)


if __name__ == "__main__":
    _print_table()
