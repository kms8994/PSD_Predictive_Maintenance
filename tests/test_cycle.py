"""psd/cycle.py 검증.

2-6: 4단계_계산공식_근거조사.md 3장 "예시 계산"(가정 날씨, 역 A/B/C)과 손계산이 일치하는지.
2-7: 계산의 기본 성질(평균 날씨 역이면 10년, 습도가 오르면 IFM 주기가 줄어듦, 기본값이 범위 안에 있음).
"""
from datetime import date

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
    fixed_cycle,
    judge,
    single_cause_cycle,
)

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
