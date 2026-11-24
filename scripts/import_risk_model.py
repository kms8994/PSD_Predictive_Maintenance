"""데스크탑에서 미리 학습한 AI 위험도 모델을 앱에 넣는다 (2026-11-24, scripts/pretrain_risk_model.py의 짝).

1. 파일이 이 앱과 맞는지 본다: 소분류 코드 순서·특징 목록이 같아야 하고, scikit-learn 버전이 같아야 한다(다르면 못 읽거나
   값이 달라질 수 있다).
2. 앱 DB의 기록으로 채점한다: 최근 6개월 스냅샷(정답이 다 보이는 달)에서 이 모델과 기준 방법(수명 사용 비율 순), 지금 모델을 겨룬다.
3. 기준 방법보다 낫고 최소 기준(AUC 0.55)을 넘으면 "지금 모델"로 넣는다. 아니면 기록만 남기고 지금 모델을 그대로 둔다(--force면 넣는다).
   넣은 모델은 risk_models에 trigger="pretrained"로 1줄, 파일은 models/risk_v{번호}.joblib.

그 뒤 기록을 올리면 자동 재학습이 앱 기록으로 학습한 새 모델과 이 모델을 같은 기간으로 겨뤄, 새 모델이 지지 않을 때만 바꾼다.

    python scripts/import_risk_model.py models/pretrained/risk_pretrained_20261124_20벌.joblib
    python scripts/import_risk_model.py <파일> --dry-run   # 채점만, DB에 안 넣음
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAR_PAST = date(2000, 1, 1)  # 앱 기록은 하나도 보지 않았다는 뜻(재학습 비교에서 앱 기록 전 기간이 "새 기간"이 된다)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", help="models/pretrained/....joblib")
    ap.add_argument("--force", action="store_true", help="채점 결과와 상관없이 지금 모델로 넣는다")
    ap.add_argument("--dry-run", action="store_true", help="채점만 하고 DB에 넣지 않는다")
    a = ap.parse_args()

    import joblib
    import sklearn

    from psd import risk
    from psd.db import Base, engine, get_session
    from psd.models import RiskModel
    from scripts.pretrain_risk_model import taxonomy_key

    path = Path(a.file)
    bundle = joblib.load(path)
    if not isinstance(bundle, dict) or bundle.get("meta", {}).get("kind") != "psd-risk-pretrained":
        sys.exit("scripts/pretrain_risk_model.py가 만든 파일이 아닙니다")
    model, meta = bundle["model"], bundle["meta"]
    problems = []
    if meta["unit_codes"] != risk.PART_CODES:
        problems.append(f"소분류 목록이 다릅니다(파일 {meta['units']}개, 분류표 {meta['taxonomy']}; 앱 {len(risk.PART_CODES)}개) — "
                        "두 컴퓨터의 data/private/장애분류.json이 같은지 확인하세요")
    elif meta.get("taxonomy_key") != taxonomy_key():
        problems.append("소분류 이름이 다릅니다 — 두 컴퓨터의 분류표 파일이 같은지 확인하세요")
    if meta["features"] != risk.FEATURE_NAMES:
        problems.append("특징 목록이 다릅니다 — 두 컴퓨터의 코드(git 커밋)가 같은지 확인하세요")
    if meta["sklearn"] != sklearn.__version__:
        problems.append(f"scikit-learn 버전이 다릅니다(파일 {meta['sklearn']}, 여기 {sklearn.__version__}) — requirements.txt대로 맞추세요")
    if problems:
        sys.exit("가져올 수 없습니다:\n- " + "\n- ".join(problems))

    v = meta["metrics"]["val_lines"]
    print(f"{path.name}: 가상 노선 {meta['lines']}벌, {meta['train_rows']:,}행, 만든 때 {meta['created_at']}")
    print(f"  데스크탑 채점(학습에 안 쓴 벌): AUC {v['auc']:.3f}, 기준 방법 {meta['metrics']['baseline']['auc']:.3f}")

    Base.metadata.create_all(engine)
    session = get_session()
    try:
        fb = risk.FeatureBuilder.load(session)
        snaps = risk.label_complete_snapshots(fb.data_end())
        if not snaps:
            sys.exit("앱 DB에 채점할 기록이 없습니다(정답이 다 보이는 달이 없음)")
        holdout = snaps[-risk.VAL_MONTHS:]
        mine = risk.score_on(model, fb, holdout)
        base = risk.baseline_on(fb, holdout)
        champ_row = risk.active_row(session)
        champ_model = risk.load_model(champ_row.id) if champ_row else None
        champ = risk.score_on(champ_model, fb, holdout) if champ_model is not None else None
        print(f"  앱 기록 채점({holdout[0]} ~ {holdout[-1]}): 이 모델 AUC {mine['auc']:.3f} · 기준 방법 {base['auc']:.3f}"
              + (f" · 지금 모델 v{champ_row.id} {champ['auc']:.3f}(그 기간을 학습에 썼을 수 있음 — 참고만)" if champ else ""))
        # 지금 모델은 이 기간을 학습에 썼을 수 있어 겨루지 않는다 — 최소 기준과 기준 방법만 넘으면 넣는다
        accept, reason = risk.decide(mine, base, None, False)
        if accept:
            reason = f"앱 기록 최근 {len(holdout)}개월 AUC {mine['auc']:.3f}, 기준 방법 {base['auc']:.3f}보다 나아 적용"
        if a.force and not accept:
            accept, reason = True, f"--force로 넣음({reason})"
        reason = f"데스크탑 사전 학습(가상 노선 {meta['lines']}벌) 가져옴 — " + reason
        print(f"  → {'지금 모델로 넣음' if accept else '넣지 않음'}: {reason}")
        if a.dry_run:
            print("--dry-run이라 DB에 넣지 않았습니다")
            return

        row = RiskModel(trained_at=datetime.now().replace(microsecond=0), trigger="pretrained",
                        data_key=f"pretrained:{path.name}"[:200], data_end=FAR_PAST, last_train_snapshot=FAR_PAST,
                        train_rows=meta["train_rows"], status="active" if accept else "rejected", reason=reason[:300],
                        metrics={"holdout": [holdout[0].isoformat(), holdout[-1].isoformat()], "holdout_is_new": True,
                                 "challenger": mine, "champion": champ, "baseline": base,
                                 "pretrained": {"file": path.name, "lines": meta["lines"], "data_end": meta["data_end"],
                                                "val_lines": meta["metrics"]["val_lines"]}},
                        importance=meta["importance"], file_name="")
        session.add(row)
        session.flush()
        risk.MODEL_DIR.mkdir(parents=True, exist_ok=True)
        joblib.dump(model, risk._model_file(row.id))
        row.file_name = risk._model_file(row.id).name
        if accept and champ_row is not None:
            champ_row.status = "retired"
        session.commit()
        print(f"저장: risk_models v{row.id}, {row.file_name}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
