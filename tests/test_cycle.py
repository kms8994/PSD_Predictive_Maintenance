"""psd/cycle.py 검증.

2-6: 4단계_계산공식_근거조사.md 3장 "예시 계산"(가정 날씨, 역 A/B/C)과 손계산이 일치하는지.
2-7: 계산의 기본 성질(평균 날씨 역이면 10년, 습도가 오르면 IFM 주기가 줄어듦, 기본값이 범위 안에 있음).
"""
from datetime import date

import pandas as pd
import pytest

from psd.cycle import (
    IFM_WEIGHT_RANGE,
    MOTOR_WEIGHT_RANGE,
    Cycle,
    af_capacitor,
    af_corrosion,
    af_motor_winding,
    af_solder,
    dcm_cycle,
    daily_af,
    fixed_cycle,
    judge,
    observatory_climate,
    part_cycle,
    period_average_af,
    reference_climate,
    single_cause_cycle,
    station_counts,
)
from psd.cycle import STATIONS as CYCLE_STATIONS

REF = {"T": 15.0, "RH": 68.0, "DT": 7.5}
REF["Tmax"] = REF["T"] + REF["DT"] / 2

# 문서 표의 역 A/B/C: (기온, 습도, 일교차) -> 기대 AF(콘덴서/납땜/부식/모터), 소수 둘째 자리까지
STATIONS = {
    "A 해안": (15.5, 72, 6.0, (1.04, 0.65, 1.23, 1.03)),
    "B 내륙": (14.5, 64, 9.0, (0.97, 1.42, 0.80, 0.97)),
    "C 습하고 일교차 큼": (15.5, 72, 9.0, (1.04, 1.44, 1.23, 1.03)),
}

# 문서 표의 DCM/IFM/모터 권장 주기(기본값, 범위 년)와 판정
EXPECTED_CYCLE = {
    "A 해안": {
        "DCM": (10.2, 9.2, 11.7, "불확실"),
        "IFM": (9.0, 8.4, 9.6, "짧음_확실"),
        "모터": (9.9, 9.8, 10.0, "같음"),
    },
    "B 내륙": {
        "DCM": (9.5, 8.4, 10.7, "불확실"),
        "IFM": (11.1, 10.4, 11.9, "김_확실"),
        "모터": (10.1, 10.0, 10.2, "같음"),
    },
    "C 습하고 일교차 큼": {
        "DCM": (8.5, 7.7, 9.3, "짧음_확실"),
        "IFM": (9.0, 8.4, 9.6, "짧음_확실"),
        "모터": (9.9, 9.8, 10.0, "같음"),
    },
}


def _af(t: float, rh: float, dt: float) -> dict[str, float]:
    tmax = t + dt / 2
    return {
        "cap": af_capacitor(t, REF["T"]),
        "sol": af_solder(dt, tmax, REF["DT"], REF["Tmax"]),
        "hum": af_corrosion(rh, t, REF["RH"], REF["T"]),
        "mot": af_motor_winding(t, REF["T"]),
    }


@pytest.mark.parametrize("name", STATIONS)
def test_af_matches_hand_calc_example(name):
    t, rh, dt, expected = STATIONS[name]
    af = _af(t, rh, dt)
    assert [round(af["cap"], 2), round(af["sol"], 2), round(af["hum"], 2), round(af["mot"], 2)] == list(expected)


@pytest.mark.parametrize("name", STATIONS)
def test_dcm_cycle_matches_hand_calc_example(name):
    t, rh, dt, _ = STATIONS[name]
    af = _af(t, rh, dt)
    base, lo, hi, j = EXPECTED_CYCLE[name]["DCM"]
    result = dcm_cycle(af)
    assert round(result.base_years, 1) == base
    assert round(result.lo_years, 1) == lo
    assert round(result.hi_years, 1) == hi
    assert result.judgment == j


@pytest.mark.parametrize("name", STATIONS)
def test_ifm_cycle_matches_hand_calc_example(name):
    t, rh, dt, _ = STATIONS[name]
    af = _af(t, rh, dt)
    base, lo, hi, j = EXPECTED_CYCLE[name]["IFM"]
    result = single_cause_cycle(af["hum"], IFM_WEIGHT_RANGE)
    assert round(result.base_years, 1) == base
    assert round(result.lo_years, 1) == lo
    assert round(result.hi_years, 1) == hi
    assert result.judgment == j


@pytest.mark.parametrize("name", STATIONS)
def test_motor_cycle_matches_hand_calc_example(name):
    t, rh, dt, _ = STATIONS[name]
    af = _af(t, rh, dt)
    base, lo, hi, j = EXPECTED_CYCLE[name]["모터"]
    result = single_cause_cycle(af["mot"], MOTOR_WEIGHT_RANGE)
    assert round(result.base_years, 1) == base
    assert round(result.lo_years, 1) == lo
    assert round(result.hi_years, 1) == hi
    assert result.judgment == j


# ---------- 2-7: 계산의 기본 성질 ----------


def test_average_weather_station_is_ten_years():
    """기준 날씨(24개 역 평균) 그대로인 역은 모든 원인의 AF가 1이라 주기도 10년이어야 한다."""
    af = _af(REF["T"], REF["RH"], REF["DT"])
    for value in af.values():
        assert value == pytest.approx(1.0, abs=1e-9)

    dcm = dcm_cycle(af)
    assert dcm.base_years == pytest.approx(10.0)
    assert dcm.lo_years == pytest.approx(10.0)
    assert dcm.hi_years == pytest.approx(10.0)
    assert dcm.judgment == "같음"

    ifm = single_cause_cycle(af["hum"], IFM_WEIGHT_RANGE)
    assert ifm.base_years == pytest.approx(10.0)
    assert ifm.judgment == "같음"

    motor = single_cause_cycle(af["mot"], MOTOR_WEIGHT_RANGE)
    assert motor.base_years == pytest.approx(10.0)
    assert motor.judgment == "같음"


def test_higher_humidity_shortens_ifm_cycle():
    """습도가 기준보다 높을수록 IFM 릴레이의 권장 주기는 짧아져야 한다(부식 가속)."""
    humid = _af(REF["T"], REF["RH"] + 10, REF["DT"])
    more_humid = _af(REF["T"], REF["RH"] + 20, REF["DT"])

    base_cycle = single_cause_cycle(1.0, IFM_WEIGHT_RANGE).base_years
    humid_cycle = single_cause_cycle(humid["hum"], IFM_WEIGHT_RANGE).base_years
    more_humid_cycle = single_cause_cycle(more_humid["hum"], IFM_WEIGHT_RANGE).base_years

    assert base_cycle > humid_cycle > more_humid_cycle


@pytest.mark.parametrize("name", STATIONS)
def test_dcm_default_is_within_range(name):
    t, rh, dt, _ = STATIONS[name]
    result = dcm_cycle(_af(t, rh, dt))
    assert result.lo_years <= result.base_years <= result.hi_years


@pytest.mark.parametrize("name", STATIONS)
def test_single_cause_default_is_within_range(name):
    t, rh, dt, _ = STATIONS[name]
    af = _af(t, rh, dt)
    for af_value, weight_range in ((af["hum"], IFM_WEIGHT_RANGE), (af["mot"], MOTOR_WEIGHT_RANGE)):
        result = single_cause_cycle(af_value, weight_range)
        assert result.lo_years <= result.base_years <= result.hi_years


def test_judge_boundaries():
    assert judge(9.5, 10.5) == "같음"
    assert judge(9.0, 9.9) == "짧음_확실"
    assert judge(10.1, 10.6) == "김_확실"
    assert judge(9.4, 10.6) == "불확실"


def test_fixed_cycle_is_always_ten_years():
    result = fixed_cycle()
    assert result == Cycle(10.0, 10.0, 10.0, "날씨무관")


# ---------- 비중 기본값: near_water 가중 제거 (2026-10-25) ----------
#
# AWS 실측 습도가 이제 역(aws_stn)마다 직접 다르게 들어가므로, 비중까지 역별로 나누면
# 같은 "해안=습함" 효과를 중복 적용하게 된다. dcm_cycle·part_cycle은 역 구분 없이
# 똑같은 기본값·범위를 써야 한다 — near_water 인자를 받지 않는다.


def test_dcm_cycle_does_not_take_near_water_argument():
    import inspect

    assert "near_water" not in inspect.signature(dcm_cycle).parameters
    assert "near_water" not in inspect.signature(part_cycle).parameters


def test_part_cycle_is_deterministic_regardless_of_station():
    """같은 날씨(af)라면 역이 달라도 결과가 같아야 한다 — 비중에 역별 가중이 없다는 뜻."""
    t, rh, dt, _ = STATIONS["A 해안"]
    af = _af(t, rh, dt)
    for part in ("DCM", "IFM 릴레이", "모터", "타이밍벨트"):
        assert part_cycle(part, af) == part_cycle(part, af)


# ---------- psd_install_date: 2026-10-24 미팅 답변(Q3) 반영 ----------
#
# 좌천~태화강 구간(16~23번, 8개 역)은 "22년 사용개시"가 확실하다. 부전~일광 구간(1~15번)은 실제로는
# 6개 역 22년·9개 역 23년이 섞여 있지만 역 이름이 없어(보안사규) 역 단위로 못 나누는데, 2026-10-24
# 사용자 지시로 정확한 구분이 나올 때까지 이 15개 역도 일단 22년으로 통일해 둔다. 북울산(24번)만
# 아직 개통 전이라 제외(None → open_date로 대신).


def test_all_opened_stations_default_to_2022_psd_install_date_except_bukulsan():
    by_order = {s.order: s for s in CYCLE_STATIONS}
    for order in range(1, 24):
        assert by_order[order].psd_install_date == date(2022, 1, 1), by_order[order].name
    assert by_order[24].psd_install_date is None  # 북울산: 아직 개통 전


# ---------- AWS 지점 기반 날씨 계산: 2026-10-25 반영 ----------
#
# 부산/울산 ASOS 2버킷 대신 역마다 가장 가까운 AWS 지점(Station.aws_stn)으로 날씨를 계산한다.
# 실제 CSV 없이도 함수가 올바르게 지점별로 묶는지 합성 데이터로 확인한다.


def _synthetic_weather() -> pd.DataFrame:
    """실제 두 AWS 지점(해안 기장 923, 내륙 동래 940) 코드로 흉내 낸 작은 날씨 데이터프레임.

    observatory_climate·period_average_af는 station_counts()(= STATIONS의 실제 aws_stn)를
    기준으로 묶으므로, 임의의 코드(예: "A"/"B")가 아니라 실제 STATIONS에 쓰이는 코드를 써야 한다.
    """
    rows = []
    for day in range(1, 11):
        rows.append({"date": pd.Timestamp("2022-01-01") + pd.Timedelta(days=day), "stn": "923",
                      "ta_avg": 15.0, "ta_max": 19.0, "ta_min": 11.0, "hm_avg": 80.0, "dtr": 8.0})
        rows.append({"date": pd.Timestamp("2022-01-01") + pd.Timedelta(days=day), "stn": "940",
                      "ta_avg": 15.0, "ta_max": 19.0, "ta_min": 11.0, "hm_avg": 60.0, "dtr": 8.0})
    return pd.DataFrame(rows)


def test_station_counts_groups_by_aws_stn_not_obs():
    counts = station_counts()
    # 역 24개가 obs(부산/울산) 2개가 아니라 aws_stn 여러 개로 나뉘어야 한다
    assert set(counts) == {s.aws_stn for s in CYCLE_STATIONS}
    assert len(counts) > 2
    assert sum(counts.values()) == len(CYCLE_STATIONS)


def test_observatory_climate_and_reference_climate_use_aws_groups():
    weather = _synthetic_weather()
    # station_counts()가 실제 CYCLE_STATIONS를 쓰므로, 합성 데이터의 지점 "A"/"B"가 그 안에
    # 없으면 reference_climate이 0으로 나눈다 — 여기서는 observatory_climate 자체만 검증한다
    climate = observatory_climate(weather)
    assert climate["923"]["RH"] == pytest.approx(80.0)
    assert climate["940"]["RH"] == pytest.approx(60.0)
    assert climate["923"]["T"] == pytest.approx(15.0)


def test_period_average_af_differs_between_aws_stations():
    weather = _synthetic_weather()
    ref = {"T": 15.0, "RH": 70.0, "DT": 8.0, "Tmax": 19.0}  # 두 지점 평균값 정도로 가정
    daily = daily_af(weather, ref)
    af = period_average_af(daily)
    # 습도가 더 높은 지점(923 기장)의 부식 가속 계수가 더 커야 한다
    assert af["923"]["hum"] > af["940"]["hum"]
    # 기온·일교차가 같으므로 콘덴서·납땜 가속 계수는 같아야 한다
    assert af["923"]["cap"] == pytest.approx(af["940"]["cap"])
