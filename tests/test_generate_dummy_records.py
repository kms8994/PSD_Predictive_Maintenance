"""scripts/generate_dummy_records.py(v2, 2026-11-16) 검증: 조건부 와이블·가상 나이·사건 순서. DB 없이 돈다."""
import math
import random
import statistics
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.generate_dummy_records import (  # noqa: E402
    FAIL_ACTIONS,
    PREV_ACTIONS,
    PartSim,
    UnitContext,
    cumulative,
    simulate_unit,
)

LONG = cumulative(np.ones(365 * 200))  # 200년 — 사실상 중도절단 없음


def test_first_failure_has_the_weibull_mean():
    """새 부품(가상 나이 0)의 첫 고장까지 평균 = 척도 × Γ(1 + 1/β)."""
    rng = random.Random(0)
    scale, shape = 6.0, 1.6
    days = []
    for _ in range(4000):
        p = PartSim("DCM", scale, shape, LONG)
        p.resample(rng)
        days.append(p.next_fail / 365.25)
    assert abs(statistics.mean(days) / (scale * math.gamma(1 + 1 / shape)) - 1) < 0.03


def test_renew_sets_virtual_age_by_action():
    rng = random.Random(1)
    for action, factor in (("full", 0.0), ("partial", 0.4), ("overhaul", 0.3), ("repair", 1.0)):
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
    a, b = PartSim("IFM", 6.0, 1.3, LONG), PartSim("IFM", 6.0, 1.3, fast)
    a.resample(rng1)
    b.resample(rng2)
    assert abs(b.next_fail - a.next_fail / 2) <= 1


def test_simulated_unit_events_are_valid_upload_rows():
    """한 문의 사건이 날짜 순이고, 같은 날 손본 부품은 점검 줄에 안 들어가며, 엑셀로 쓰면 업로드 규칙을 통과한다."""
    from psd.catalog import DOOR_PART_CODES
    from psd.excel_io import CODE_TO_NAME, REASON_CODE_TO_LABEL, detail_labels, parse_upload, rows_to_excel_bytes

    rng = random.Random(4)
    cum = cumulative(np.ones(365 * 5))
    parts = [PartSim(c, 3.0, 2.0, cum) for c in DOOR_PART_CODES]  # 짧은 수명으로 사건을 많이
    inspections = list(range(400, 365 * 5, 182))
    events = simulate_unit(parts, inspections, rng, UnitContext()).events
    assert events and [e.day for e in events] == sorted(e.day for e in events)
    assert {e.reason for e in events} >= {"fail", "check"}
    for day in {e.day for e in events}:
        same = [e for e in events if e.day == day]
        checked = [c for e in same if e.reason == "check" for c in e.parts]
        touched = [c for e in same if e.reason != "check" for c in e.parts]
        assert not set(checked) & set(touched)

    rows = [{"역": "부전", "방향": "부전 방면", "문번호": "1-1", "작업일": f"{2022 + e.day // 366}-01-01",
             "사유": REASON_CODE_TO_LABEL[e.reason], "부품": ",".join(CODE_TO_NAME[c] for c in e.parts),
             **detail_labels(e.action, e.subs, e.symptom, e.cause), "입력자": "시험"} for e in events]
    parsed = parse_upload(rows_to_excel_bytes(rows))
    assert [p.action_code for p in parsed] == [e.action for e in events]


def test_action_tables_are_consistent():
    """확률 합 1, 일부 교체엔 세부 부품이 있고, 세부 부품은 그 부품의 것."""
    from psd.catalog import SUB_PART_OF

    for table in (FAIL_ACTIONS, PREV_ACTIONS):
        for code, rows in table.items():
            assert math.isclose(sum(p for _a, p, _s in rows), 1.0)
            for action, _p, subs in rows:
                if action == "partial":
                    assert subs
                assert all(SUB_PART_OF[s] == code for s, _w in subs)
