"""다른 숨은 정답으로 AI 위험도 모델 검증 — "넣은 정답 다시 찾기"인지 확인 (2026-11-26, 데스크탑 작업).

scripts/pretrain_risk_model.py는 20벌 모두 같은 숨은 정답(--truth-seed 2026)으로 만들고, 채점 2벌도 같은 정답이다.
부품 종류별 진짜 수명(make_truth)이 같으니 모델이 "이 세계의 소분류별 고장률"을 외워도 점수가 높게 나온다.
여기서는 정답 자체를 바꿔 다섯 가지를 비교한다.

  A. 정답 2026(앱 더미)      — --model 사전 학습 모델을 앱 더미와 같은 정답·새 사건 벌에
                               (고정정답 모델이면 학습 때와 같은 정답이라 점수 재현, 섞은 정답 모델이면 본 적 없는 정답)
  B. 정답 5000~              — 같은 모델을 학습에 없던 정답 벌에 (옮겨 가는지)
  C. 그 정답으로 학습        — 정답마다 같은 정답의 다른 벌로 학습해 채점 (그 세계 기록이 있을 때의 상한 근처)
  D. 정답 섞어 사전 학습     — 벌마다 정답 씨앗을 다르게 학습 → 본 적 없는 정답에 (고치는 방법 후보)
  E. D에서 소분류 특징 뺌    — 소분류별 고장률 외우기를 막으면 어떤지

    python scripts/check_truth_transfer.py --jobs 8
결과: 화면 + data/processed/truth_transfer.json. 숫자는 모두 시뮬레이터 기준이다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.pretrain_risk_model import line_dataset  # noqa: E402

TRAIN_TRUTH = 2026


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="models/pretrained/risk_pretrained_20261124_20벌.joblib")
    ap.add_argument("--today", default="2026-11-24")
    ap.add_argument("--test-truths", type=int, default=6, help="채점할 다른 정답 개수")
    ap.add_argument("--mix-lines", type=int, default=18, help="D·E 학습 벌 수(벌마다 정답 다름)")
    ap.add_argument("--step", type=int, default=1, help="스냅샷 간격(개월)")
    ap.add_argument("--jobs", type=int, default=8)
    a = ap.parse_args()

    import joblib
    from sklearn.ensemble import HistGradientBoostingClassifier

    from psd.risk import FEATURE_NAMES, _scores, new_model

    today = date.fromisoformat(a.today)
    pre = joblib.load(ROOT / a.model)["model"]
    test_truths = [5000 + i for i in range(a.test_truths)]
    mix_truths = [7000 + i for i in range(a.mix_lines)]

    # (이름, r, 사건 씨앗, 정답 씨앗). r은 벌 번호(역·문 번호 띄우기용)라 겹치지 않게
    jobs = [("A", 0, 900, TRAIN_TRUTH), ("A", 1, 901, TRAIN_TRUTH)]
    for i, tt in enumerate(test_truths):
        jobs += [("B", 10 + 3 * i, 910 + 3 * i, tt), ("Ctr", 11 + 3 * i, 911 + 3 * i, tt), ("Ctr", 12 + 3 * i, 912 + 3 * i, tt)]
    for i, tt in enumerate(mix_truths):
        jobs.append(("D", 100 + i, 1000 + i, tt))

    t0 = time.time()
    data = []
    with ProcessPoolExecutor(max_workers=a.jobs) as ex:
        for (tag, r, seed, tt), res in zip(jobs, ex.map(line_dataset, [(r, s, tt, today, a.step) for _, r, s, tt in jobs])):
            data.append((tag, tt, res))
            print(f"  {len(data)}/{len(jobs)} {tag} 정답 {tt}: 행 {len(res[1]):,}, 사후조치 {res[4]:,} ({time.time() - t0:.0f}초)", flush=True)

    def score(model, res, cols=None):
        X, y, t = res[0], res[1], res[2]
        p = model.predict_proba(X if cols is None else X[:, cols])[:, 1]
        return _scores(y, p, t)

    def base(res):
        return _scores(res[1], res[0][:, FEATURE_NAMES.index("vage_used")], res[2])

    by = lambda tag: [(tt, res) for g, tt, res in data if g == tag]  # noqa: E731
    out: dict = {"today": a.today, "step": a.step, "model": a.model, "rows": {}}

    out["rows"]["A"] = [score(pre, res) | {"truth": tt, "baseline": base(res)} for tt, res in by("A")]
    out["rows"]["B"] = [score(pre, res) | {"truth": tt, "baseline": base(res)} for tt, res in by("B")]

    print("C: 정답마다 그 정답의 2벌로 학습", flush=True)
    out["rows"]["C"] = []
    for tt, res in by("B"):
        tr = [r for t2, r in by("Ctr") if t2 == tt]
        m = new_model().fit(np.concatenate([r[0] for r in tr]), np.concatenate([r[1] for r in tr]))
        out["rows"]["C"].append(score(m, res) | {"truth": tt})

    Xd = np.concatenate([r[0] for _, r in by("D")])
    yd = np.concatenate([r[1] for _, r in by("D")])
    print(f"D: 정답 {len(mix_truths)}개 섞어 {len(yd):,}행 학습", flush=True)
    md = new_model().fit(Xd, yd)
    out["rows"]["D"] = [score(md, res) | {"truth": tt} for tt, res in by("B")]

    cols = [i for i, f in enumerate(FEATURE_NAMES) if f != "part"]
    print("E: D에서 소분류 특징 빼고 학습", flush=True)
    me = HistGradientBoostingClassifier(learning_rate=0.03, max_iter=200, max_leaf_nodes=7, min_samples_leaf=300,
                                        l2_regularization=1.0, random_state=0).fit(Xd[:, cols], yd)
    out["rows"]["E"] = [score(me, res, cols) | {"truth": tt} for tt, res in by("B")]

    print()
    print(f"{'':28}{'AUC':>8}{'상위100':>9}{'기준 AUC':>10}")
    labels = {"A": f"A 정답 {TRAIN_TRUTH}(앱 더미)", "B": "B 정답 5000~(--model)", "C": "C 그 정답으로 학습",
              "D": "D 정답 섞어 사전 학습", "E": "E D에서 소분류 특징 뺌"}
    summary = {}
    for k, name in labels.items():
        rows = out["rows"][k]
        auc = float(np.mean([r["auc"] for r in rows]))
        top = float(np.mean([r["top_k_precision"] for r in rows]))
        b = [r["baseline"]["auc"] for r in rows if "baseline" in r]
        summary[k] = {"auc": auc, "auc_min": float(min(r["auc"] for r in rows)), "auc_max": float(max(r["auc"] for r in rows)),
                      "top100": top, "baseline_auc": float(np.mean(b)) if b else None}
        print(f"{name:28}{auc:8.3f}{top:9.1%}{(f'{np.mean(b):10.3f}' if b else ''):>10}"
              f"   (정답별 {summary[k]['auc_min']:.3f}~{summary[k]['auc_max']:.3f})")
    out["summary"] = summary
    path = ROOT / "data" / "processed" / "truth_transfer.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n저장: {path.relative_to(ROOT)} · 전체 {time.time() - t0:.0f}초")


if __name__ == "__main__":
    main()
