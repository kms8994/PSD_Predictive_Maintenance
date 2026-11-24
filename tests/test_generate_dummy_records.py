"""scripts/generate_dummy_records.py(v3, 2026-11-24) 검증: 조건부 와이블·가상 나이·사건 순서·양식 줄. DB 없이 돈다.

소분류 코드는 예시 분류표와 실제 분류표에 함께 있는 것(MOTOR, BELT, HANGER, DCU, PSC)만 쓴다.
"""
import math
import random
import statistics
import sys
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.generate_dummy_records import (  # noqa: E402
    AGE_FACTOR,
    FAIL_ACTIONS,
    PREV_ACTIONS,
    PartSim,
    cumulative,
    make_truth,
    simulate_unit,
)

LONG = cumulative(np.ones(365 * 200))  # 200년 — 사실상 중도절단 없음


def test_first_failure_has_the_weibull_mean():
    """새 부품(가상 나이 0)의 첫 고장까지 평균 = 척도 × Γ(1 + 1/β)."""
    rng = random.Random(0)
    scale, shape = 6.0, 1.6
    days = []
    for _ in range(4000):
        p = PartSim("DCU", scale, shape, LONG)
        p.resample(rng)
        days.append(p.next_fail / 365.25)
    assert abs(statistics.mean(days) / (scale * math.gamma(1 + 1 / shape)) - 1) < 0.03


def test_renew_sets_virtual_age_by_action():
    rng = random.Random(1)
    for action, factor in AGE_FACTOR.items():
        p = PartSim("MOTOR", 8.0, 2.0, LONG)
        p.renew(3650, action, rng)  # 10년째(평평한 날씨라 유효 나이 = 달력 나이)
        assert math.isclose(p.v, 3650 / 365.25 * factor)
        assert p.last == 3650 and p.next_fail > 3650


def test_minimal_repair_brings_the_next_failure_sooner_than_replacement():
    """마모 부품(β>1)을 고쳐 쓰면(나이 그대로) 새것으로 바꿀 때보다 다음 고장이 빨리 온다."""
    rng = random.Random(2)
    gaps = {"full": [], "repair": []}
    for action in gaps:
        for _ in range(2000):
            p = PartSim("BELT", 8.0, 3.0, LONG)
            p.renew(3000, action, rng)
            gaps[action].append(p.next_fail - 3000)
    assert statistics.mean(gaps["repair"]) < statistics.mean(gaps["full"]) * 0.7


def test_weather_multiplier_speeds_up_aging():
    """나이를 두 배로 먹는 날씨면 고장도 (달력으로) 절반쯤에 온다."""
    rng1, rng2 = random.Random(3), random.Random(3)
    fast = cumulative(np.full(365 * 200, 2.0))
    a, b = PartSim("DCU", 6.0, 1.3, LONG), PartSim("DCU", 6.0, 1.3, fast)
    a.resample(rng1)
    b.resample(rng2)
    assert abs(b.next_fail - a.next_fail / 2) <= 1


def test_simulated_unit_events_are_valid_upload_rows():
    """한 문의 사건이 날짜 순이고, 양식 줄로 쓰면 업로드 규칙을 통과한다(조치·장애인지가 그대로 읽힘)."""
    from psd.excel_io import parse_upload, record_row, rows_to_excel_bytes

    rng = random.Random(4)
    cum = cumulative(np.ones(365 * 4))
    codes = ["MOTOR", "BELT", "HANGER", "DCU"]
    parts = [PartSim(c, 2.0, 2.0, cum, last=0) for c in codes]  # 짧은 수명으로 사건을 많이
    events = simulate_unit(parts, list(range(400, 365 * 4, 182)), rng)
    assert events and [e.day for e in events] == sorted(e.day for e in events)
    assert {e.reason for e in events} >= {"fail"}
    day0 = date(2022, 1, 1)
    rows = [record_row(day=date.fromordinal(day0.toordinal() + e.day), station="부전", reason=e.reason, unit_code=e.parts[0],
                       action=e.action, side="U", door_no="1-1") for e in events]
    parsed = parse_upload(rows_to_excel_bytes(rows)).rows
    assert [(p.action_code, p.reason_code, p.unit_code) for p in parsed] == [(e.action, e.reason, e.parts[0]) for e in events]


def test_action_tables_are_probabilities_over_known_actions():
    from psd.catalog import ACTIONS

    for rows in (*FAIL_ACTIONS.values(), PREV_ACTIONS):
        assert math.isclose(sum(p for _a, p in rows), 1.0)
        assert all(a in ACTIONS for a, _p in rows)
    assert set(AGE_FACTOR) == set(ACTIONS)


def test_truth_is_fixed_by_truth_seed_and_covers_every_unit():
    """가상 노선 여러 벌이 같은 정답을 쓰도록 정답은 truth_seed로만 정해진다."""
    from psd.catalog import PART_TYPES

    a, b, c = make_truth(2026), make_truth(2026), make_truth(7)
    assert a == b and a != c
    assert set(a) == {p["code"] for p in PART_TYPES}
    for p in PART_TYPES:
        t = a[p["code"]]
        assert ("bias" in t) == (p["formula"] is not None)  # 공식이 있으면 공식 대비, 없으면 평균 수명을 직접
        assert 1.0 <= t["shape"] <= 3.5
