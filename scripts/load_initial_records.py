"""(선택) 더미 정비 기록을 DB에 넣는다 — 새 컴퓨터에서 지금 화면과 같은 상태를 만들 때.

data/dummy/초기데이터_정비기록_<N>건.xlsx는 실제 고장 데이터가 없어 scripts/generate_dummy_records.py로 만든
더미 기록이다(2026-11-16 v2 — 부품마다 일생을 따라가며 고장·수리·교체·점검을 만들고, 숨은 정답은
data/dummy/정답_수명모수.json에 따로 둔다. 데이터셋_v2_설계.md). 개발 PC의 DB와 같다. 시연용 최근 두 달치는
넣지 않고 정비기록_샘플_<N>건.xlsx로 남겨 "정비기록 시연" 탭에서 올려 본다.

넣지 않으면 교체 기록이 0건이라 모든 부품이 "안전"으로 나온다(README "초기 데이터" 참고).
교체 기록이 이미 있으면 겹치지 않게 아무것도 하지 않는다(--replace면 기록·업로드 이력을 모두 지우고 넣는다 —
더미만 들어 있는 개발 DB에서만 쓴다). 화면에서 올린 것과 똑같이 업로드 이력이 1줄 남아서, 교체 기록 탭의
업로드 이력에서 되돌릴 수도 있다. 실제 운영에서는 쓰지 않는다.

    python scripts/load_initial_records.py
    python scripts/load_initial_records.py --replace   # 예전 더미(1,230건 등)를 지우고 새 더미로
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, func, select, update  # noqa: E402

from psd.api import _sync  # noqa: E402
from psd.calibrate import PSD_START  # noqa: E402
from psd.db import get_session  # noqa: E402
from psd.models import Part, ReplacementPart, ReplacementRecord, UploadBatch  # noqa: E402


def initial_records_path() -> Path:
    files = sorted((ROOT / "data" / "dummy").glob("초기데이터_정비기록_*건.xlsx"))
    if len(files) != 1:
        raise SystemExit(f"data/dummy/초기데이터_정비기록_*건.xlsx가 하나여야 합니다: {[f.name for f in files]}")
    return files[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replace", action="store_true", help="있는 기록·업로드 이력을 모두 지우고 넣는다(개발 DB 전용)")
    args = ap.parse_args()
    path = initial_records_path()
    session = get_session()
    try:
        existing = session.scalar(select(func.count(ReplacementRecord.id)))
        if existing and not args.replace:
            print(f"교체 기록이 이미 {existing}건 있어 넣지 않았습니다(겹침 방지). 바꾸려면 --replace")
            return
        if existing:
            session.execute(delete(ReplacementPart))
            session.execute(delete(ReplacementRecord))
            session.execute(delete(UploadBatch))
            for station_id, start in PSD_START.items():
                session.execute(update(Part).where(Part.station_id == station_id).values(install_date=start, has_record=False))
            print(f"지움: 기록 {existing}건과 업로드 이력, 부품 설치일은 PSD 사용개시일로")
        result = _sync(session, path.read_bytes(), path.name, delete_missing=False)
        session.commit()
        print(f"넣음: {path.name} {result['inserted']}건")
    finally:
        session.close()


if __name__ == "__main__":
    main()
