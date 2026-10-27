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

    python -m psd.cycle          # 24개 역 x 7종 결과 표 출력
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
WEATHER_CSV = ROOT / "data" / "processed" / "weather_daily.csv"  # ASOS(부산/울산), 더는 계산에 안 씀 — 아래 AWS_WEATHER_CSV 참고
AWS_WEATHER_CSV = ROOT / "data" / "processed" / "aws_weather_daily.csv"  # AWS, 2026-10-25부터 날씨 계산의 기준

K_EV = 8.617e-5  # 볼츠만 상수 (eV/K)
REPLACEMENT_YEARS = 10.0  # 제조사 권장 주기 (기준 기후에서)


def kelvin(celsius: float) -> float:
    return celsius + 273.15


# ---------- 역 목록 (설계_v2.md "역 목록", obs는 목업과 동일한 가정) ----------


@dataclass(frozen=True)
class Station:
    order: int
    name: str
    open_date: date  # 철도(동해선) 개통일 — 역사적 사실, 김민수 제공(2026-10-14)
    obs: str  # "부산" 또는 "울산" (ASOS 코드, scripts/load_weather.py의 DB 적재용·역 화면 표시용으로만 남음)
    near_water: bool = False  # 역 근처 바다·하천 인접 여부 (2026-10-24 미팅 답변 Q5 근거, 아래 주석 참고)
    psd_install_date: date | None = None  # 스크린도어(PSD) 실제 사용개시일. None이면 open_date로 대신한다
    # (아래 psd_install_date 주석 참고) — 2026-10-24 미팅 답변 Q3 근거, open_date와 다를 수 있음.
    aws_stn: str = ""  # 역에서 가장 가까운 AWS(방재기상관측) 지점 코드 — 날씨 계산은 전부 이걸로 한다
    in_service: bool = True  # PSD가 실제로 설치·운영 중인지. False면 scripts/init_db.py가 문·부품을 만들지 않는다
    # (open_date가 미래로 지나가도 자동으로 True가 되지 않는다 — 북울산처럼 개통일 자체가
    # 미정 추정값인 역을 날짜 비교만으로 "운영 중"으로 잘못 판정하지 않기 위한 명시적 플래그.
    # 코드 리뷰로 찾은 버그: 이 플래그가 없던 시절엔 _D4(2026-09-01, 북울산 임시 개통일)를
    # 오늘 날짜가 지나자마자 다른 23개 역과 똑같이 "안전" 상태로 보여주고 있었다.)


_D1 = date(2016, 12, 30)
_D2 = date(2020, 3, 28)
_D3 = date(2021, 12, 28)
_D4 = date(2026, 9, 1)  # 북울산: 개통일 미정, 월초로 임시 처리
_PSD_2 = date(2022, 1, 1)  # PSD 사용개시 추정(2022년, 월일 미상→연초로 임시 처리)

# psd_install_date: 설계_v2.md는 지금까지 "개통일 = 스크린도어 설치일"로 가정해 왔다(철도가 뚫린
# 날과 PSD가 실제로 쓰이기 시작한 날을 같다고 봄). 2026-10-24 미팅 답변(Q3)으로 실제로는 다르다는
# 게 확인됐다: "보안사규상 사용기간만 포괄적으로 답변드립니다 - (부전~오시리아 구간) 22년 사용개시
# 6개역사, 23년 사용개시 9개역사 - (좌천~태화강 구간) 22년 사용개시 8개역사" (hwp 원문 그대로).
#
# 좌천~태화강 구간(16~23번, 8개 역)은 전원 22년이라 확실하다. 부전~일광 구간(1~15번, hwp가
# "부전~오시리아 구간"으로 부른 1차 노선 15개 역 전체로 보인다 — 6+9=15로 정확히 맞아떨어진다)은
# 6개 역이 22년·9개 역이 23년으로 섞여 있는데, 보안사규상 어느 역인지 이름이 없어 역 단위로 나눌
# 근거가 없다. **2026-10-24 사용자 지시로, 정확한 구분이 나올 때까지 이 15개 역도 일단 22년으로
# 통일해 둔다**(실제로는 이 중 9개 역이 23년일 수 있어 그 역들의 주기는 다소 길게 잡힐 수 있음 —
# 코레일에 역 이름 목록을 추가로 확인해야 하는 항목, 설계_v2.md 할 일 참고). 북울산(24번)은 아직
# 개통 전(open_date 자체가 미정 추정값)이라 제외 — PSD가 역보다 먼저 쓰였을 수는 없다.
STATIONS: list[Station] = [
    Station(1, "부전", _D1, "부산", near_water=False, psd_install_date=_PSD_2, aws_stn="940"),
    Station(2, "거제해맞이", _D1, "부산", near_water=False, psd_install_date=_PSD_2, aws_stn="940"),
    Station(3, "거제", _D1, "부산", near_water=False, psd_install_date=_PSD_2, aws_stn="940"),
    Station(4, "교대", _D1, "부산", near_water=False, psd_install_date=_PSD_2, aws_stn="940"),
    Station(5, "동래", _D1, "부산", near_water=False, psd_install_date=_PSD_2, aws_stn="940"),
    Station(6, "안락", _D1, "부산", near_water=False, psd_install_date=_PSD_2, aws_stn="940"),
    Station(7, "부산원동", _D2, "부산", near_water=False, psd_install_date=_PSD_2, aws_stn="937"),
    Station(8, "재송", _D1, "부산", near_water=False, psd_install_date=_PSD_2, aws_stn="937"),
    Station(9, "센텀", _D1, "부산", near_water=True, psd_install_date=_PSD_2, aws_stn="937"),
    Station(10, "벡스코", _D1, "부산", near_water=True, psd_install_date=_PSD_2, aws_stn="937"),
    Station(11, "신해운대", _D1, "부산", near_water=True, psd_install_date=_PSD_2, aws_stn="937"),
    Station(12, "송정", _D1, "부산", near_water=True, psd_install_date=_PSD_2, aws_stn="937"),
    Station(13, "오시리아", _D1, "부산", near_water=True, psd_install_date=_PSD_2, aws_stn="923"),
    Station(14, "기장", _D1, "부산", near_water=True, psd_install_date=_PSD_2, aws_stn="923"),
    Station(15, "일광", _D1, "부산", near_water=True, psd_install_date=_PSD_2, aws_stn="923"),
    Station(16, "좌천", _D3, "부산", near_water=False, psd_install_date=_PSD_2, aws_stn="923"),
    Station(17, "월내", _D3, "부산", near_water=True, psd_install_date=_PSD_2, aws_stn="923"),
    Station(18, "서생", _D3, "울산", near_water=True, psd_install_date=_PSD_2, aws_stn="924"),
    Station(19, "남창", _D3, "울산", near_water=False, psd_install_date=_PSD_2, aws_stn="954"),
    Station(20, "망양", _D3, "울산", near_water=False, psd_install_date=_PSD_2, aws_stn="954"),
    Station(21, "덕하", _D3, "울산", near_water=False, psd_install_date=_PSD_2, aws_stn="954"),
    Station(22, "개운포", _D3, "울산", near_water=True, psd_install_date=_PSD_2, aws_stn="898"),
    Station(23, "태화강", _D3, "울산", near_water=True, psd_install_date=_PSD_2, aws_stn="898"),
    Station(24, "북울산", _D4, "울산", near_water=False, aws_stn="943", in_service=False),
]

# aws_stn: 역 → 가장 가까운 AWS 지점을 행정구역 기준으로 매핑한 것이다(2026-10-25, 실측 좌표
# 비교는 아님 — 코레일/멘토 확인 전까지 가정). 부전~동래/안락(연제·동래구) → 동래(940),
# 부산원동~송정(해운대구) → 해운대(937), 오시리아~월내(기장군) → 기장(923), 서생(울주군
# 서생면, 간절곶 바로 옆) → 간절곶(924), 남창~덕하(울주군 온양·온산읍) → 온산(954),
# 개운포·태화강(울산 남구) → 장생포(898), 북울산(울산 북구, 아직 미개통) → 매곡(943).
AWS_STATION_NAMES: dict[str, str] = {
    "160": "부산(레)", "296": "북부산", "904": "사상", "910": "영도", "921": "가덕도",
    "923": "기장", "937": "해운대", "938": "부산진", "939": "금정구", "940": "동래",
    "941": "북구", "942": "부산남구", "950": "사하",
    "854": "삼동", "898": "장생포", "900": "두서", "901": "울기", "924": "간절곶",
    "943": "매곡", "949": "정자", "954": "온산",
}

# 코드 리뷰로 찾은 버그: STATIONS의 aws_stn과 AWS_STATION_NAMES는 둘 다 손으로 관리하는
# 별개 블록이라, 둘이 어긋나도(새 역 추가 시 aws_stn 오타, aws_stn 기본값 ""을 안 채움 등)
# 아무도 바로 알아채지 못했다 — psd/api.py 여러 곳에서 AWS_STATION_NAMES[aws_stn]을 쓸 때마다
# KeyError로 흩어져서 터지게 된다. 모듈을 불러오는 시점에 한 번에 검사해 바로 에러를 낸다.
_unknown_aws_stn = {s.aws_stn for s in STATIONS} - set(AWS_STATION_NAMES)
if _unknown_aws_stn:
    raise ValueError(
        f"STATIONS에 AWS_STATION_NAMES에 없는 aws_stn 코드가 있습니다: {_unknown_aws_stn} "
        f"(역 목록에 aws_stn을 안 채웠거나 오타일 수 있습니다)"
    )

# near_water: 공개 지리 정보(역 주변 해변·포구·하천 유무)로 추정한 값이다. 2026-10-24 미팅에서
# 코레일 담당자가 "역사별 환경(바다 인접)에 따라 특정 부품의 장애건수는 명확히 구분됨"이라고
# 답변했지만, 정확한 역별 목록은 받지 못했다(보안사규상 상세 데이터 제공 불가, 미팅/[답변]...hwp
# 질문 5). 위 STATIONS의 near_water는 역 이름·위치로 추정한 임시값이며, 코레일/멘토 확인 전까지는
# 가정으로 다룬다. 확실한 역: 센텀·벡스코(수영강 하구), 신해운대·송정·오시리아·일광(해수욕장
# 인접), 월내(포구), 서생(해안 면), 개운포(온산만 하구), 태화강(태화강 바로 옆). 그 외는 내륙으로
# 보고 false.
#
# **2026-10-25: 비중 기본값 계산에는 더 이상 안 쓴다.** near_water는 원래 "역별 실측 습도가
# 없으니 바다 인접 여부로 비중을 대신 옮기자"는 임시방편이었다. 이제 AWS 실측 습도가
# aws_stn별로 이미 다르게 들어가(위 AWS_STATION_NAMES, 4단계_계산공식_근거조사.md 2-1) 같은
# 효과를 가속 계수(af_hum) 쪽에서 직접 낸다 — 비중까지 또 옮기면 같은 "해안=습함" 효과를 두 번
# 적용하는 중복이 된다. near_water 필드 자체는 역 화면 표시·참고용으로만 남겨 둔다.

OBS_CODE = {"부산": "159", "울산": "152"}  # ASOS 코드. 이제 날씨 계산에는 안 쓰고 DB 적재(scripts/load_weather.py)·역 화면 표시용으로만 남음

PARTS = ["모터", "타이밍벨트", "풀리", "도어 행거", "DCM", "IFM 릴레이", "종합제어반 릴레이"]
WEATHER_PARTS = {"DCM", "IFM 릴레이", "모터"}

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
# 2-6). load_weather()/WEATHER_CSV(ASOS)는 scripts/load_weather.py의 DB 적재(daily_weather
# 표, 실제로 읽는 화면 없음)에만 남겨 두고, 실제 계산은 load_aws_weather()를 쓴다.


def load_weather() -> pd.DataFrame:
    df = pd.read_csv(WEATHER_CSV, parse_dates=["date"])
    df["stn"] = df["stn"].astype(str)
    return df


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
    """날씨와 무관한 부품(타이밍벨트·풀리·도어 행거·종합제어반 릴레이): 10년 유지."""
    return Cycle(REPLACEMENT_YEARS, REPLACEMENT_YEARS, REPLACEMENT_YEARS, "날씨무관")


def part_cycle(part: str, af: dict[str, float]) -> Cycle:
    if part == "DCM":
        return dcm_cycle(af)
    if part == "IFM 릴레이":
        return single_cause_cycle(af["hum"], IFM_WEIGHT_RANGE)
    if part == "모터":
        return single_cause_cycle(af["mot"], MOTOR_WEIGHT_RANGE)
    return fixed_cycle()


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
    """24개 역 x 부품 7종의 권장 주기(2-8). scripts/compute_cycles.py가 이 결과를 DB에 저장한다."""
    weather = load_aws_weather()
    ref = reference_climate(weather)
    af_by_stn = period_average_af(daily_af(weather, ref))
    return [
        StationPartCycle(station.name, part, part_cycle(part, af_by_stn[station.aws_stn]))
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
        }
        for r in compute_all()
    ]
    df = pd.DataFrame(rows)
    with pd.option_context("display.max_rows", None, "display.width", 160):
        print(df)


if __name__ == "__main__":
    _print_table()
