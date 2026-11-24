"""주기 보정이 숨은 정답에 얼마나 가까운지 잰다 (2026-11-16, 데이터셋_v2_설계.md 6장).

DB의 정비 기록(더미)으로 보정한 주기를 data/private/dummy/정답_수명모수.json의 진짜 평균 수명과 비교한다.
v1(형상 2 고정, 전체 교체만 셈)과 v2(psd/calibrate.py — 형상·공식 치우침 추정, 수리 반영)를 나란히 본다.
더미는 실데이터가 아니므로 "시뮬레이터 가정이 맞다면"의 성능이다.

기준일은 정답 파일의 cutoff(초기 데이터의 마지막 날)로 둔다 — DB엔 그날까지 기록만 있는데 오늘로 세면
아직 안 올린 두 달 동안 "고장 없이 버텼다"로 잘못 센다.

    python scripts/check_calibration.py
"""
import json
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from psd.calibrate import PRIOR_FAILURES, PSD_START, calibration_map, part_intervals  # noqa: E402
from psd.cycle import STATIONS  # noqa: E402
from psd.db import get_session  # noqa: E402
from psd.models import CycleResult, Part, ReplacementPart, ReplacementRecord  # noqa: E402

ANSWER = ROOT / "data" / "private" / "dummy" / "정답_수명모수.json"  # v3(2026-11-24): 분류표가 비공개라 더미도 private


def v1_map(session, today: date) -> dict:
    """2026-11-16 전 방식: 형상 2 고정, 전체 교체만 수명 끝/시작으로 보고 수리·일부 교체 기록은 버림."""
    formula = {(c.station_id, c.part_type_code): c.base_years for c in session.execute(select(CycleResult)).scalars()}
    recs = defaultdict(list)
    for sid, door, code, day, reason, action in session.execute(
            select(ReplacementRecord.station_id, ReplacementRecord.door_id, ReplacementPart.part_type_code,
                   ReplacementRecord.replaced_on, ReplacementRecord.reason, ReplacementRecord.action)
            .join(ReplacementPart, ReplacementPart.record_id == ReplacementRecord.id)):
        if action == "full":
            recs[(sid, door, code)].append((day, reason, action))
    sums = defaultdict(lambda: [0, 0.0])
    for sid, door, code in session.execute(select(Part.station_id, Part.door_id, Part.part_type_code)):
        for _v, x, failed in part_intervals(PSD_START[sid], recs.get((sid, door, code), []), today):
            sums[(sid, code)][0] += failed
            sums[(sid, code)][1] += x * x
    g = math.gamma(1.5)
    out = {}
    for key, f in formula.items():
        n, s2 = sums[key]
        b0 = PRIOR_FAILURES * (f / g) ** 2
        out[key] = g * math.sqrt((b0 + s2) / (PRIOR_FAILURES + n))
    return out


def main() -> None:
    truth = json.loads(ANSWER.read_text(encoding="utf-8"))
    today = date.fromisoformat(truth["cutoff"])
    sid = {s.name: s.id for s in STATIONS}
    session = get_session()
    try:
        v2 = calibration_map(session, today=today, use_cache=False)
        v1 = v1_map(session, today)
    finally:
        session.close()

    by_part = defaultdict(list)
    for r in truth["station_part"]:
        key = (sid[r["station"]], r["part"])
        if key in v2:
            by_part[r["part"]].append((r, v1[key], v2[key]))

    err = lambda est, t: abs(est / t - 1)  # noqa: E731
    print(f"기준일 {today} · 정답과 비교(역 x 부품 평균, 년). 오차 = |추정/정답 − 1|의 평균\n")
    print("| 부품 | 공식 | 정답 | v1 | v2 | 오차 공식 | 오차 v1 | 오차 v2 | 80% 범위에 정답 | β 정답 | β 추정(80%) | 치우침 정답 | 치우침 추정 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    all_rows = []
    for code, rows in by_part.items():
        all_rows += rows
        c0 = rows[0][2]
        cover = sum(c.lo_years <= r["true_mean_years"] <= c.hi_years for r, _a, c in rows)
        print(f"| {code} | {mean(r['formula_years'] for r, *_ in rows):.2f} | {mean(r['true_mean_years'] for r, *_ in rows):.2f} "
              f"| {mean(a for _r, a, _c in rows):.2f} | {mean(c.raw_years for *_x, c in rows):.2f} "
              f"| {mean(err(r['formula_years'], r['true_mean_years']) for r, *_ in rows):.0%} "
              f"| {mean(err(a, r['true_mean_years']) for r, a, _c in rows):.0%} "
              f"| {mean(err(c.raw_years, r['true_mean_years']) for r, _a, c in rows):.0%} "
              f"| {cover}/{len(rows)} | {truth['units'][code]['shape']} | {c0.shape:.2f} ({c0.shape_lo:.2f}~{c0.shape_hi:.2f}) "
              f"| {truth['units'][code].get('bias', '공식 없음')} | {c0.bias:.2f} |")
    t = lambda r: r["true_mean_years"]  # noqa: E731
    print(f"\n전체 오차: 공식 {mean(err(r['formula_years'], t(r)) for r, *_ in all_rows):.1%} · "
          f"v1 {mean(err(a, t(r)) for r, a, _c in all_rows):.1%} · v2 {mean(err(c.raw_years, t(r)) for r, _a, c in all_rows):.1%}")
    print(f"80% 범위가 정답을 담은 비율: {sum(c.lo_years <= t(r) <= c.hi_years for r, _a, c in all_rows) / len(all_rows):.0%}")


if __name__ == "__main__":
    main()
