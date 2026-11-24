"""가상 노선 여러 벌로 AI 위험도 모델을 미리 학습한다 — **데스크탑에서 돌리는 작업** (2026-11-24, 데이터셋_v3_설계.md 7장).

앱 DB에는 동해선 한 벌 분량의 기록만 있어 모델이 배울 고장이 적다. 그래서 더미 생성기로 같은 동해선을 여러 번 겪게 하고
(숨은 정답은 같고 사건만 다름) 그 전부로 학습한다. 결과 파일을 노트북으로 가져가 scripts/import_risk_model.py로 넣으면
앱이 그 모델을 "지금 모델"로 쓰고, 실제 기록이 올라오면 그 기록으로 학습한 새 모델이 이걸 이길 때만 바꾼다.

DB가 필요 없다(.env 없이 돈다). 필요한 것: 이 저장소, requirements.txt, 분류표 data/private/장애분류.json, 날씨 CSV(저장소에 있음).
더미로 잰 점수는 "시뮬레이터 가정이 맞다면"의 성능이다 — 실제 성능처럼 말하지 않는다.

    python scripts/pretrain_risk_model.py --lines 2 --step 3      # 시간 재기용(몇 분)
    python scripts/pretrain_risk_model.py --lines 20 --jobs 8     # 본 학습

결과: models/pretrained/risk_pretrained_<날짜>_<벌>벌.joblib (+ 같은 이름 .json 요약). 둘 다 노트북으로 옮긴다.

시간·메모리(어림): 벌마다 부품 약 1만 개 × 스냅샷 약 45개월 = 45만 행. 20벌이면 900만 행, 특징 만들기가 벌마다 몇 분
(--jobs로 나눠 돈다), 학습 몇 분, 메모리 1~2GB.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "models" / "pretrained"
STATION_STRIDE = 1000  # 벌마다 역 번호를 이만큼 띄운다(같은 역끼리 묶는 특징이 벌 사이에 섞이지 않게)
DOOR_STRIDE = 100_000


def taxonomy_key() -> str:
    """분류표가 같은지 확인하는 짧은 값: 소분류 코드·이름 순서."""
    from psd.catalog import PART_TYPES

    return hashlib.sha1(json.dumps([(p["code"], p["name"]) for p in PART_TYPES], ensure_ascii=False).encode()).hexdigest()[:12]


def line_dataset(args: tuple) -> tuple[np.ndarray, np.ndarray, np.ndarray, int, int]:
    """가상 노선 한 벌: 시뮬레이션 → 부품 이력 → 스냅샷 특징. (X, y, 스냅샷, 부품 수, 사후조치 수)."""
    r, seed, truth_seed, today, step = args
    from psd.calibrate import PSD_START
    from psd.risk import FeatureBuilder, PartHistory, label_complete_snapshots
    from scripts.generate_dummy_records import inputs_without_db, simulate_all

    inputs = inputs_without_db()
    units, _truth, day0 = simulate_all(inputs, today, seed, truth_seed)
    parts, pid, fails = [], 0, 0
    for u in units:
        sid = r * STATION_STRIDE + u.station.order
        door = r * DOOR_STRIDE + u.door.id if u.door else None
        for ps in u.parts:
            ev = sorted((day0 + timedelta(days=e.day), e.reason, e.action) for e in u.events if e.parts[0] == ps.code)
            fails += sum(1 for e in ev if e[1] == "fail")
            pid += 1
            parts.append(PartHistory(pid, sid, door, ps.code, ev, PSD_START[u.station.order]))
    formula = {(r * STATION_STRIDE + sid, code): v for (sid, code), v in inputs.base.items()}
    fb = FeatureBuilder(parts, formula)
    snaps = label_complete_snapshots(today)[::step]
    X, y, t = fb.dataset(snaps)
    return X.astype(np.float32), y.astype(np.int8), t, len(parts), fails


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lines", type=int, default=20, help="가상 노선 벌 수")
    ap.add_argument("--val-lines", type=int, default=2, help="채점용으로 학습에서 뺄 벌 수")
    ap.add_argument("--seed", type=int, default=100, help="첫 벌의 사건 씨앗(벌마다 +1)")
    ap.add_argument("--truth-seed", type=int, default=2026, help="숨은 정답 씨앗 — 앱 더미와 같게 둔다")
    ap.add_argument("--today", default=date.today().isoformat(), help="가상 기록의 마지막 날")
    ap.add_argument("--step", type=int, default=1, help="스냅샷 간격(개월). 2면 행이 반으로")
    ap.add_argument("--jobs", type=int, default=1, help="동시에 만들 벌 수(CPU 코어 수 이하)")
    a = ap.parse_args()
    if a.lines <= a.val_lines:
        sys.exit("--lines가 --val-lines보다 커야 합니다")

    from psd.catalog import PART_TYPES, TAXONOMY
    from psd.risk import FEATURE_NAMES, FEATURES, HORIZON_DAYS, PART_CODES, _scores, new_model

    if TAXONOMY.source != "private":
        print(f"주의: 실제 분류표가 아니라 {TAXONOMY.source} 분류표로 학습합니다 — 노트북 앱과 분류표가 같아야 가져올 수 있습니다")
    today = date.fromisoformat(a.today)
    t0 = time.time()
    jobs = [(r, a.seed + r, a.truth_seed, today, a.step) for r in range(a.lines)]
    results = []
    with ProcessPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        for r, res in zip(range(a.lines), ex.map(line_dataset, jobs)):
            results.append(res)
            print(f"  {r + 1}/{a.lines}벌: 행 {len(res[1]):,}, 부품 {res[3]:,}, 사후조치 {res[4]:,} ({time.time() - t0:.0f}초)", flush=True)

    tr, va = results[: a.lines - a.val_lines], results[a.lines - a.val_lines:]
    stack = lambda rs, i: np.concatenate([x[i] for x in rs])  # noqa: E731
    Xtr, ytr = stack(tr, 0), stack(tr, 1)
    Xva, yva, tva = stack(va, 0), stack(va, 1), stack(va, 2)
    print(f"학습 {len(ytr):,}행 / 채점 {len(yva):,}행(학습에 안 쓴 {a.val_lines}벌) — 특징 만들기 {time.time() - t0:.0f}초", flush=True)

    t1 = time.time()
    val_model = new_model().fit(Xtr, ytr)
    p = val_model.predict_proba(Xva)[:, 1]
    # 채점: 벌마다 같은 달 스냅샷이 겹치므로 "벌 x 달"을 한 묶음으로 상위 100개를 센다
    groups = tva + np.repeat(np.arange(len(va)), [len(x[1]) for x in va]) * 10_000_000
    ml = _scores(yva, p, groups)
    base = _scores(yva, Xva[:, FEATURE_NAMES.index("vage_used")], groups)
    from sklearn.inspection import permutation_importance

    sub = np.random.default_rng(0).choice(len(yva), size=min(len(yva), 200_000), replace=False)
    imp = permutation_importance(val_model, Xva[sub], yva[sub], scoring="roc_auc", n_repeats=3, random_state=0)
    importance = sorted(((FEATURES[i][1], float(imp.importances_mean[i])) for i in range(len(FEATURES))), key=lambda x: -x[1])
    print(f"채점(학습에 안 쓴 벌): AI AUC {ml['auc']:.3f} · 기준 방법(수명 사용 비율) {base['auc']:.3f} · "
          f"상위 100개 중 실제 고장 {ml['top_k_precision']:.1%} — {time.time() - t1:.0f}초", flush=True)

    final = new_model().fit(np.concatenate([Xtr, Xva]), np.concatenate([ytr, yva]))
    import joblib
    import sklearn

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    name = f"risk_pretrained_{today:%Y%m%d}_{a.lines}벌"
    meta = {
        "kind": "psd-risk-pretrained", "created_at": datetime.now().isoformat(timespec="seconds"), "lines": a.lines,
        "val_lines": a.val_lines, "seed": a.seed, "truth_seed": a.truth_seed, "data_end": today.isoformat(), "step": a.step,
        "train_rows": int(len(ytr) + len(yva)), "unit_codes": PART_CODES, "features": FEATURE_NAMES, "horizon_days": HORIZON_DAYS,
        "taxonomy": TAXONOMY.source, "taxonomy_key": taxonomy_key(), "units": len(PART_TYPES),
        "sklearn": sklearn.__version__, "python": sys.version.split()[0],
        "metrics": {"val_lines": ml, "baseline": base}, "importance": importance,
    }
    joblib.dump({"model": final, "meta": meta}, OUT_DIR / f"{name}.joblib", compress=3)
    (OUT_DIR / f"{name}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"저장: {(OUT_DIR / f'{name}.joblib').relative_to(ROOT)} (+ .json) · 전체 {time.time() - t0:.0f}초")
    print("노트북으로 두 파일을 옮긴 뒤: python scripts/import_risk_model.py models/pretrained/" + f"{name}.joblib")


if __name__ == "__main__":
    main()
