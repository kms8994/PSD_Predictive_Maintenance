"""(선택) 더미 교체 기록 1,230건을 DB에 넣는다 — 새 컴퓨터에서 지금 화면과 같은 상태를 만들 때.

data/dummy/초기데이터_교체기록_1230건.xlsx는 실제 고장 데이터가 없어 신뢰성 공식 주기를 평균으로 한
와이블 분포로 뽑은 더미 기록이다(scripts/generate_dummy_records.py로 2026-10-25에 만든 것, 개발 PC의
DB와 똑같다). 이 스크립트로 다시 만들지 않고 파일을 넣는 이유: 생성 결과가 그때의 주기·설치일에 따라
달라져 지금 다시 돌리면 같은 기록이 안 나온다.

넣지 않으면 교체 기록이 0건이라 모든 부품이 "안전"으로 나온다(README "초기 데이터" 참고).
교체 기록이 이미 있으면 겹치지 않게 아무것도 하지 않는다.

    python scripts/load_initial_records.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402

from psd.api import _apply_upload  # noqa: E402
from psd.db import get_session  # noqa: E402
from psd.models import ReplacementRecord  # noqa: E402

INITIAL_RECORDS = ROOT / "data" / "dummy" / "초기데이터_교체기록_1230건.xlsx"


def main() -> None:
    session = get_session()
    try:
        existing = session.scalar(select(func.count(ReplacementRecord.id)))
        if existing:
            print(f"교체 기록이 이미 {existing}건 있어 넣지 않았습니다(겹침 방지).")
            return
        result = _apply_upload(session, INITIAL_RECORDS.read_bytes())
        session.commit()
        print(f"넣음: {INITIAL_RECORDS.name} {result['inserted']}건")
    finally:
        session.close()


if __name__ == "__main__":
    main()
