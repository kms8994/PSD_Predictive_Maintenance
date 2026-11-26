"""동해선 PSD 부품 교체주기 계산.

근거: 설계_v2.md 4단계, 4단계_계산공식_근거조사.md.
기준 주기(10년)에 역의 날씨로 구한 가속 계수(AF)를 나눠 권장 주기를 낸다.
고장 원인별 비중은 근거가 없으므로 기본값과 함께 범위를 계산해 판정한다.

2026-10-25: 날씨 계산을 ASOS 부산/울산 2버킷에서 AWS(방재기상관측) 지점 7곳(동래·해운대·
기장·간절곶·온산·장생포·매곡) 기준으로 바꿨다(설계_v2.md 2-6, Station.aws_stn). 기간도
PSD 실제 사용 기간에 맞춰 2022~2025년으로 바뀌었다(scripts/preprocess_aws_weather.py).
실제로 받아보니 습도가 해안 지점(기장 68.9%)과 내륙 지점(동래 63.7%)에서 꾸준히 5%p 정도
차이가 나, 2026-10-24 미팅 답변(Q5) "역사별 환경(바다 인접)에 따라 장애건수가 구분됨"이
실측으로도 확인됐다.

2026-10-24 미팅 답변(Q5) "역사별 환경(바다 인접)에 따라 장애건수가 구분됨"은 처음엔 역별
near_water(바다·하천 인접) 플래그로 DCM·IFM 릴레이 비중의 기본값을 옮기는 식으로 반영했었다.
2026-10-25: AWS 실측 습도가 이제 지점별로 직접 다르게 들어가 같은 효과를 중복으로 내고
있었던 걸 확인하고 비중 가중은 걷어냈다 — near_water는 역 화면 표시용으로만 남아 있다(아래
near_water 주석).

    python -m psd.cycle          # 24개 역 x 소분류 결과 표 출력
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
AWS_WEATHER_CSV = ROOT / "data" / "processed" / "aws_weather_daily.csv"  # AWS, 2026-10-25부터 날씨 계산의 기준

K_EV = 8.617e-5  # 볼츠만 상수 (eV/K)
REPLACEMENT_YEARS = 10.0  # 제조사 권장 주기 (기준 기후에서)


def kelvin(celsius: float) -> float:
    return celsius + 273.15


# ---------- 역 목록 ----------
# 2026-11-24: 역 목록(개통일·PSD 사용개시일·AWS 지점·운영·바다 인접 추정)은 고정 엑셀 data/stations/동해선_역목록.xlsx로
# 옮겼다(psd/stations.py). 값의 근거(미팅 답변 Q3 "22·23년 사용개시" → 2022-01-01 통일, Q5 바다 인접 추정, AWS 지점 매핑)는
# 그 파일의 '변경 이력' 시트에 있다. near_water는 표시용이고 계산엔 안 쓴다 — AWS 실측 습도가 지점별로 이미 들어가
# 같은 "해안 = 습함" 효과를 두 번 넣지 않으려고(2026-10-25).
from psd.stations import AWS_STATION_NAMES, STATIONS  # noqa: E402

# 2026-11-24 v3: 예측 단위는 장애부위 소분류(psd/catalog.py PART_TYPES)다. 소분류마다 분류표에 적은 공식(formula)으로 계산한다:
# "motor"(모터&엔코더 ← v2 모터·풀리), "dcm"(DCU ← v2 DCM), "fixed"(날씨 무관 10년), 없음(공식 없음 — 기준 10년을 넓은
# 사전값으로만 쓰고 기록으로 배운다, psd/calibrate.py). v2의 IFM 릴레이 습도 공식은 어느 소분류인지 몰라 지금은 안 쓴다.

# 비중 범위 (4단계_계산공식_근거조사.md 3장). 근거가 없으므로 기본값은 균등, 범위는 전체
# 격자/구간으로 계산한다 — 역 성격과 무관하게 전부 같다(위 near_water 주석 참고: AWS 실측
# 습도가 이미 역별로 다르게 들어가므로 비중까지 역별로 나눌 근거가 더 없어졌다).
DCM_STEPS = (0.1, 0.2, 0.3, 0.4, 0.5)
DCM_DEFAULT = (0.25, 0.25, 0.25, 0.25)  # (콘덴서/기온, 납땜/일교차, 부식/습도, 날씨무관)

IFM_WEIGHT_RANGE = (0.2, 0.8, 0.5)  # (최소, 최대, 기본값)

MOTOR_WEIGHT_RANGE = (0.1, 0.5, 0.3)  # 모터 권선은 기온(NSWC-98 식) 근거라 역 구분 없이 유지


# ---------- 운행횟수 (2026-10-25 사용자 지시) ----------
#
# 열차 운행 1회 = 문 개폐 1회라, 운행횟수가 많을수록 기계적 마모(모터·벨트·풀리·행거 등)가
# 빨라질 거라는 건 직관적으로 맞다. 다만 역별 실제 운행 횟수 기록이 없다 — 설계_v2.md 2-2가
# "사용량은 계산에 넣지 않는다"고 미리 선을 그은 이유이기도 하다.
#
# 2026-10-25 사용자 지시로 "자료가 생기기 전까지는 변수로 만들지 말고 전 역 공통 고정값만
# 쓰자"로 정리했다: 상행·하행 실제 운행 횟수를 역별로 셀 방법이 없으니, 동해선 전체에 같은
# 배차간격을 쓰는 아래 값 하나만 참고용으로 둔다. **계산(AF)에는 반영하지 않는다** — 모든
# 역에 똑같이 곱하는 값이라 역간 비교에는 어차피 영향이 없고, 제조사가 10년 권장 주기를 낼 때
# 가정한 운행량을 모르는 채 전체 결과를 올리거나 내리면 그게 더 근거 없는 보정이 된다(이
# 프로젝트가 계속 피해온 "숫자 하나 임의로 끼워 넣기"와 같은 문제). 대신 역 화면·보고서에
# 참고 정보로만 보여주고, 역별 실제 운행 기록이 생기면 그때 원인(AF_usage)으로 추가한다.
#
# 출처: 설계_v2.md "동해선 기본 정보"(2026-10-14 검색 확인) — 배차간격 출퇴근 15분, 평시
# 25분. 역별 차이 없이 노선 전체에 같은 값을 쓴다(상세 배차표를 못 구해 이 정도로 둔다).
SERVICE_INTERVAL_MIN = {"peak": 15, "offpeak": 25}  # 분 단위, 전 역·상하행 공통(실데이터 확보 전 임시)


# ---------- 날씨 ----------
#
# 2026-10-25: 부산/울산 ASOS 2버킷 대신 AWS(방재기상관측) 지점별로 날씨를 계산한다(설계_v2.md
# 2-6). 날씨는 load_aws_weather()로 data/processed/aws_weather_daily.csv에서 읽는다(ASOS 자료와
# 수집·적재 스크립트는 2026-11-01에 정리해 지웠다).


def load_aws_weather() -> pd.DataFrame:
    """AWS 일별 집계(scripts/preprocess_aws_weather.py 결과, 2022~2025년)."""
    df = pd.read_csv(AWS_WEATHER_CSV, parse_dates=["date"])
    df["stn"] = df["stn"].astype(str)
    return df


def station_counts() -> dict[str, int]:
    """AWS 지점별로 몇 개 역이 연결돼 있는지 (기준 기후 가중 평균에 쓴다)."""
    counts: dict[str, int] = {}
    for s in STATIONS:
        counts[s.aws_stn] = counts.get(s.aws_stn, 0) + 1
    return counts


def observatory_climate(weather: pd.DataFrame) -> dict[str, dict[str, float]]:
    """AWS 지점별 평균 기후(2022~2025). 부품 상세 화면의 "날씨 근거"에 쓴다."""
    result = {}
    for stn in station_counts():
        g = weather[weather["stn"] == stn]
        if g.empty:
            # 코드 리뷰로 찾은 버그: 여기서 조용히 넘어가면 g["ta_avg"].mean() 등이 NaN을
            # 돌려주고, 그 NaN이 reference_climate()의 가중 평균(합산)을 전부 오염시켜
            # compute_all()이 24개 역 x 7개 부품 전부를 아무 에러 없이 NaN으로 계산해 버린다.
            raise ValueError(
                f"AWS 지점 {stn}({AWS_STATION_NAMES.get(stn, '?')})의 날씨 데이터가 없습니다 — "
                f"{AWS_WEATHER_CSV.name}에 해당 지점이 빠졌거나 STATIONS의 aws_stn이 잘못됐을 수 있습니다."
            )
        result[stn] = {
            "T": g["ta_avg"].mean(),
            "RH": g["hm_avg"].mean(),
            "DT": g["dtr"].mean(),
            "Tmax": g["ta_max"].mean(),
        }
    return result


def reference_climate(weather: pd.DataFrame) -> dict[str, float]:
    """기준 날씨(T_ref, RH_ref, DT_ref, Tmax_ref) = 24개 역 평균 기후.

    역마다 가장 가까운 AWS 지점(Station.aws_stn)의 평균을, 그 지점에 연결된 역 수로
    가중 평균한다.
    """
    counts = station_counts()
    total = sum(counts.values())
    per_stn = observatory_climate(weather)
    return {
        key: sum(per_stn[stn][key] * counts[stn] for stn in counts) / total
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


def period_average_af_for(daily: pd.DataFrame, stn: str) -> dict[str, float]:
    """한 AWS 지점의 기간 평균 가속 계수.

    `daily`가 이미 그 지점 하나로 걸러진 경우(시뮬레이터처럼 지점 하나만 날씨를
    바꿔 다시 계산할 때)에도 다른 지점 데이터 없이 바로 쓸 수 있도록 지점 하나만
    따로 계산한다. 7개 지점을 한 번에 계산하는 `period_average_af`가 이 함수를 쓴다.
    """
    g = daily[daily["stn"] == stn]
    if g.empty:  # observatory_climate()와 같은 이유 — 빈 그룹을 그냥 두면 NaN이 조용히 퍼진다
        raise ValueError(f"AWS 지점 {stn}({AWS_STATION_NAMES.get(stn, '?')})의 날씨 데이터가 없습니다.")
    return {
        "cap": g["af_cap"].mean(),
        "sol": g["af_sol"].mean(),
        "hum": g["af_hum"].mean(),
        "mot": g["af_mot"].mean(),
    }


def period_average_af(daily: pd.DataFrame) -> dict[str, dict[str, float]]:
    """AWS 지점별 기간 평균 가속 계수 (AF_m(s) = 일별 AF_m(s, d)의 평균)."""
    return {stn: period_average_af_for(daily, stn) for stn in station_counts()}


# ---------- 2-3, 2-4. 비중 합성·범위, 판정 ----------


@dataclass(frozen=True)
class Cycle:
    """권장 주기 계산 결과. DB 모델 `psd.models.CycleResult`와는 다른, 이름 겹침을 피한 순수 계산값."""

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


def dcm_cycle(af: dict[str, float]) -> Cycle:
    """DCM: 콘덴서/납땜/부식/날씨무관 4원인, 10%~50% 격자 전체를 범위로 계산."""
    values = []
    for a, b, c in itertools.product(DCM_STEPS, repeat=3):
        o = 1 - a - b - c
        if 0.1 - 1e-9 <= o <= 0.5 + 1e-9:
            values.append(_years(a * af["cap"] + b * af["sol"] + c * af["hum"] + o))
    a0, b0, c0, o0 = DCM_DEFAULT
    base = _years(a0 * af["cap"] + b0 * af["sol"] + c0 * af["hum"] + o0)
    lo, hi = min(values), max(values)
    return Cycle(base, lo, hi, judge(lo, hi))


def single_cause_cycle(af_value: float, weight_range: tuple[float, float, float]) -> Cycle:
    """원인이 하나뿐인 부품(IFM 릴레이, 모터): 비중 구간의 양끝과 기본값만 계산하면 된다."""
    lo_w, hi_w, default_w = weight_range
    years_at = lambda w: _years(w * af_value + (1 - w))
    lo, hi = sorted((years_at(lo_w), years_at(hi_w)))
    base = years_at(default_w)
    return Cycle(base, lo, hi, judge(lo, hi))


def fixed_cycle() -> Cycle:
    """날씨와 무관한 소분류(밸트·스크류, 행거·레일, 종합제어반): 10년 유지."""
    return Cycle(REPLACEMENT_YEARS, REPLACEMENT_YEARS, REPLACEMENT_YEARS, "날씨무관")


def no_formula_cycle() -> Cycle:
    """공식이 없는 소분류: 기준 10년을 사전값으로만 둔다. 보정(psd/calibrate.py)이 이 값을 약하게 믿고 기록으로 옮겨 간다."""
    return Cycle(REPLACEMENT_YEARS, REPLACEMENT_YEARS, REPLACEMENT_YEARS, "공식없음")


def part_cycle(formula: str | None, af: dict[str, float]) -> Cycle:
    """formula: 분류표의 공식 이름("motor" | "dcm" | "fixed" | None)."""
    if formula == "dcm":
        return dcm_cycle(af)
    if formula == "motor":
        return single_cause_cycle(af["mot"], MOTOR_WEIGHT_RANGE)
    if formula == "humid":  # v2 IFM 릴레이 공식. 지금 분류표에선 쓰지 않는다(위 주석)
        return single_cause_cycle(af["hum"], IFM_WEIGHT_RANGE)
    if formula == "fixed":
        return fixed_cycle()
    return no_formula_cycle()


# ---------- 전체 계산 ----------
#
# "다음 교체 예정일"과 경고 상태(빨강·노랑·초록·확인 필요)는 교체 기록(DB)이 있어야 계산할 수 있어
# 여기(순수 계산, DB 없음)가 아니라 psd/status.py에 있다. 여긴 날씨로 구한 권장 주기만 낸다.


@dataclass(frozen=True)
class StationPartCycle:
    station: str
    part: str
    cycle: Cycle


def compute_all() -> list[StationPartCycle]:
    """역 x 소분류의 권장 주기(2-8). scripts/compute_cycles.py가 이 결과를 DB에 저장한다. part는 소분류 코드."""
    from psd.catalog import PART_TYPES

    weather = load_aws_weather()
    ref = reference_climate(weather)
    af_by_stn = period_average_af(daily_af(weather, ref))
    return [
        StationPartCycle(station.name, p["code"], part_cycle(p["formula"], af_by_stn[station.aws_stn]))
        for station in STATIONS
        for p in PART_TYPES
    ]


def _print_table() -> None:
    rows = [
        {
            "역": r.station,
            "부품": r.part,
            "주기(년)": round(r.cycle.base_years, 2),
            "범위": f"{r.cycle.lo_years:.1f}-{r.cycle.hi_years:.1f}",
            "판정": r.cycle.judgment,
        }
        for r in compute_all()
    ]
    df = pd.DataFrame(rows)
    with pd.option_context("display.max_rows", None, "display.width", 160):
        print(df)


if __name__ == "__main__":
    _print_table()
