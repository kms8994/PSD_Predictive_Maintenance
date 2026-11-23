"""AI 고장 위험도 모델을 다시 학습한다 (psd/risk.py 재학습 흐름).

새 모델을 학습해 지금 쓰는 모델과 같은 새 기간으로 겨루고, 지지 않을 때만 바꿔 쓴다. 결과는 DB risk_models에 1줄,
모델 파일은 models/risk_v{번호}.joblib. 화면에서 기록을 올리면 자동으로 같은 일을 하므로, 보통은 처음 한 번만 돌린다.

    python scripts/train_risk_model.py          # 기록이 지난 학습과 같으면 건너뜀
    python scripts/train_risk_model.py --force  # 같아도 다시 학습

노트북에서 30초 안팎(부품 4,400개 x 약 45개월 스냅샷, 그래디언트 부스팅). 더미 데이터 점수는 "시뮬레이터 가정이 맞다면"의 성능이다.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from psd.db import Base, engine, get_session  # noqa: E402
from psd.risk import retrain  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    Base.metadata.create_all(engine)  # risk_models 표가 없으면 만든다
    t0 = time.time()
    session = get_session()
    try:
        r = retrain(session, "script", force=args.force)
    finally:
        session.close()
    if r["skipped"]:
        print(f"건너뜀: {r['reason']} (지금 v{r['version']})")
        return
    m = r["metrics"]
    print(f"v{r['version']} {'적용' if r['accepted'] else '보류'} — {r['reason']} ({time.time() - t0:.0f}초)")
    print(f"검증 기간 {m['holdout'][0]} ~ {m['holdout'][1]}{' (지금 모델이 못 본 새 기간)' if m['holdout_is_new'] else ''}")
    print("| 방법 | AUC | 상위 100개 중 실제 고장 | 평균 대비 |")
    print("|---|---|---|---|")
    for key, label in (("challenger", "새 모델"), ("champion", "지금 모델"), ("baseline", "수명 사용 비율 순")):
        s = m[key]
        if s:
            print(f"| {label} | {s['auc']:.3f} | {s['top_k_precision']:.1%} | {s['lift']:.1f}배 |")


if __name__ == "__main__":
    main()
