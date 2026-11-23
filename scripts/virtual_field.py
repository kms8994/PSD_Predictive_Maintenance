"""가상 현장 — 기록이 쌓일수록 AI가 얼마나 좋아지는지 잰다 (2026-11-16, 파이프라인 11단계, 향후_방향_MVP이후.md 3-3).

실데이터를 못 받으므로 더미 시뮬레이터(scripts/generate_dummy_records.py, 숨은 정답 있음)를 가상 현장으로 쓴다.
2022년부터 2028년 말까지 고장·수리·점검을 만들어 두고, 분기마다 "그날까지 올라온 기록"만으로

1. 주기 보정(psd/calibrate.py)을 다시 해 숨은 정답과의 오차를 재고,
2. 위험도 모델(psd/risk.py)을 다시 학습해 **그 뒤 실제로 일어난 고장**(시뮬레이터가 이미 만들어 둔 미래)을 얼마나
   맞히는지 잰다 — 학습 때 못 본 미래로 재므로 "넣은 정답 다시 찾기"가 아니다.

정답 설정을 바꾼 여러 시나리오로 반복해, 공식이 어느 쪽으로 틀렸든 따라가는지 본다. 모델은 시나리오를 모른다.
결과는 data/processed/virtual_field.csv. 숫자는 "시뮬레이터 가정이 맞다면"이다.

    python scripts/virtual_field.py --quick       # 시나리오 1개, 반년 간격(시간 재기용)
    python scripts/virtual_field.py               # 시나리오 4개, 분기 간격
"""
import argparse
import csv
import math
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from psd.calibrate import PSD_START, fit_group, part_intervals  # noqa: E402
from psd.db import get_session  # noqa: E402
from psd.risk import FEATURE_NAMES, HORIZON_DAYS, FeatureBuilder, PartHistory, _scores, fit, label_complete_snapshots, months  # noqa: E402
from scripts.generate_dummy_records import (  # noqa: E402
    AGE_FACTOR,
    TRUE_BIAS,
    TRUE_SHAPE,
    load_inputs,
    simulate_all,
    weather_multipliers,
)

HORIZON = date(2028, 12, 31)  # 시뮬레이션 끝(마지막 기준일 뒤 6개월까지 정답이 있어야 함)
OUT = ROOT / "data" / "processed" / "virtual_field.csv"
CODES = ["MOTOR", "BELT", "PULLEY", "HANGER", "DCM", "IFM", "PSC"]

SCENARIOS = [
    ("기본", 2026, TRUE_SHAPE, TRUE_BIAS),  # 더미 데이터와 같은 정답 설정
    ("공식이 30% 길게", 11, TRUE_SHAPE, {c: 0.7 for c in CODES}),  # 실제 수명이 공식보다 짧음
    ("공식이 30% 짧게", 12, TRUE_SHAPE, {c: 1.3 for c in CODES}),
    ("우연 고장 위주(β 1.2)", 13, {c: 1.2 for c in CODES}, TRUE_BIAS),
]


@dataclass
class SimPart:
    pid: int
    station_id: int
    door_id: int | None
    code: str
    events: list  # (날짜, 사유, 조치), 점검 제외, 날짜순
    sim: object  # PartSim(진짜 척도·형상·날씨 누적)
    start: int


def flatten(units, day0: date) -> list[SimPart]:
    out, pid = [], 0
    for u in units:
        for ps in u.parts:
            ev = sorted((day0 + timedelta(days=e.day), e.reason, e.action) for e in u.events if ps.code in e.parts and e.reason != "check")
            pid += 1
            out.append(SimPart(pid, u.station.order, u.door.id if u.door else None, ps.code, ev, ps, u.start))
    return out


def histories(parts: list[SimPart], until: date | None) -> list[PartHistory]:
    return [PartHistory(p.pid, p.station_id, p.door_id, p.code, [e for e in p.events if until is None or e[0] <= until]) for p in parts]


def oracle_prob(p: SimPart, t: date, day0: date) -> float:
    """정답을 아는 위험도: 진짜 척도·형상·문 약함·가상 나이(진짜 조치 효과)·날씨로 6개월 안 고장 확률."""
    sim, ti = p.sim, (t - day0).days
    v, last = 0.0, p.start
    for d, _r, action in p.events:
        di = (d - day0).days
        if di >= ti:
            break
        v = (v + sim.cum[di] - sim.cum[last]) * AGE_FACTOR[action]
        last = di
    a0 = v + sim.cum[ti] - sim.cum[last]
    a1 = a0 + sim.cum[min(ti + HORIZON_DAYS, len(sim.cum) - 1)] - sim.cum[ti]
    return 1 - math.exp(-((a1 / sim.scale) ** sim.shape - (a0 / sim.scale) ** sim.shape))


def calib_errors(parts: list[SimPart], cutoff: date, base: dict, truth: dict) -> tuple[float, float, float, int]:
    """(공식 오차, 보정 오차, 80% 범위 적중률, 반영 고장 수) — 역 x 부품 평균."""
    true_mean = {(r["station"], r["part"]): r["true_mean_years"] for r in truth["station_part"]}
    name = {}
    groups: dict[tuple[int, str], list] = {}
    for p in parts:
        groups.setdefault((p.station_id, p.code), []).extend(
            part_intervals(PSD_START[p.station_id], [e for e in p.events if e[0] <= cutoff], cutoff))
    from psd.cycle import STATIONS
    name = {s.order: s.name for s in STATIONS}
    ef, ec, cov, nf = [], [], [], 0
    for code in CODES:
        keys = sorted(k for k in groups if k[1] == code)
        fits = fit_group([base[k] for k in keys], [groups[k] for k in keys])
        for k, f in zip(keys, fits):
            t = true_mean[(name[k[0]], code)]
            ef.append(abs(base[k] / t - 1))
            ec.append(abs(f.raw_years / t - 1))
            cov.append(f.lo_years <= t <= f.hi_years)
            nf += f.failures
    return float(np.mean(ef)), float(np.mean(ec)), float(np.mean(cov)), nf


def risk_scores(parts: list[SimPart], cutoff: date, base: dict, day0: date) -> dict | None:
    """cutoff까지의 기록으로 학습한 모델을 그 뒤 석 달(매달 1일)의 실제 6개월 안 고장으로 채점."""
    snaps = label_complete_snapshots(cutoff)
    if len(snaps) < 3:
        return None
    model, rows = fit(FeatureBuilder(histories(parts, cutoff), base), snaps)
    full = FeatureBuilder(histories(parts, None), base)
    targets = months(cutoff + timedelta(days=1), cutoff + timedelta(days=92))[:3]
    ys, pm, pb, po, ts = [], [], [], [], []
    for t in targets:
        X, y = full.rows(t)
        live = [p for p in parts if PSD_START[p.station_id] < t]
        ys.append(y)
        pm.append(model.predict_proba(X)[:, 1])
        pb.append(X[:, FEATURE_NAMES.index("vage_used")])
        po.append(np.array([oracle_prob(p, t, day0) for p in live]))
        ts.append(np.full(len(y), t.toordinal()))
    y, t = np.concatenate(ys), np.concatenate(ts)
    return {"train_rows": rows, "ml": _scores(y, np.concatenate(pm), t), "base": _scores(y, np.concatenate(pb), t),
            "oracle": _scores(y, np.concatenate(po), t)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="시나리오 1개, 반년 간격")
    ap.add_argument("--scenarios", type=int, default=len(SCENARIOS))
    ap.add_argument("--step", type=int, default=3, help="기준일 간격(개월)")
    args = ap.parse_args()
    n_sc, step = (1, 6) if args.quick else (args.scenarios, args.step)

    session = get_session()
    try:
        inputs = load_inputs(session)
    finally:
        session.close()
    day0 = min(PSD_START[s.order] for s in inputs.stations)
    weather = weather_multipliers(day0, (HORIZON - day0).days + 1)
    cutoffs = [d - timedelta(days=1) for d in months(date(2022, 7, 1), HORIZON - timedelta(days=HORIZON_DAYS + 92))][::step]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    fields = ["scenario", "cutoff", "months", "fails_known", "calib_formula_err", "calib_err", "calib_cover80",
              "ml_auc", "ml_top100", "base_auc", "base_top100", "oracle_auc", "oracle_top100", "base_rate", "train_rows"]
    t_all = time.time()
    with OUT.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for name, seed, shape, bias in SCENARIOS[:n_sc]:
            t0 = time.time()
            units, truth, _ = simulate_all(inputs, HORIZON, seed, shape, bias, weather)
            parts = flatten(units, day0)
            print(f"[{name}] 시뮬레이션 {time.time() - t0:.0f}초, 부품 {len(parts):,}개, 기준일 {len(cutoffs)}개")
            for c in cutoffs:
                t1 = time.time()
                fe, ce, cov, nf = calib_errors(parts, c, inputs.base, truth)
                r = risk_scores(parts, c, inputs.base, day0)
                row = {"scenario": name, "cutoff": c.isoformat(), "months": (c.year - 2022) * 12 + c.month,
                       "fails_known": nf, "calib_formula_err": round(fe, 4), "calib_err": round(ce, 4), "calib_cover80": round(cov, 3)}
                if r:
                    row.update(ml_auc=round(r["ml"]["auc"], 4), ml_top100=round(r["ml"]["top_k_precision"], 4),
                               base_auc=round(r["base"]["auc"], 4), base_top100=round(r["base"]["top_k_precision"], 4),
                               oracle_auc=round(r["oracle"]["auc"], 4), oracle_top100=round(r["oracle"]["top_k_precision"], 4),
                               base_rate=round(r["ml"]["base_rate"], 4), train_rows=r["train_rows"])
                w.writerow(row)
                fh.flush()
                ml = f" · ML AUC {row['ml_auc']:.3f}(정답 아는 모델 {row['oracle_auc']:.3f})" if r else ""
                print(f"  {c} 고장 {nf:,}건 · 주기 오차 공식 {fe:.1%} → 보정 {ce:.1%}{ml} · {time.time() - t1:.0f}초")
    print(f"저장: {OUT.relative_to(ROOT)} · 전체 {time.time() - t_all:.0f}초")


if __name__ == "__main__":
    main()
