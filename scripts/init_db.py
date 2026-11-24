"""DB 표 만들기 + 초기 데이터 적재 (MVP_계획.md 3-2~3-4, 2026-11-24 v3).

- 표: 역, 문, 부품 종류(= 장애부위 소분류), 부품, 정비 기록, 기록의 소분류, 업로드 이력, 계산 결과, AI 모델 이력
- 초기 데이터: 역 24개(이 중 PSD 운영 중인 역만, 지금은 23개 — 북울산 제외), 상·하 본선마다 문 16개(임시),
  소분류는 분류표 파일(psd/taxonomy.py)에서
- 부품: 운영 중인 역마다 문 × 문 단위 소분류 + 역 단위 소분류 1개씩(psd/layout.py)

    python scripts/init_db.py          # 없는 것만 만든다(다시 돌려도 안전)
    python scripts/init_db.py --reset  # 표를 지우고 다시 만든다 — v2 DB(교체 기록 7종 부품)에서 v3로 옮길 때 필요

v2 → v3: 기록 칸과 예측 단위가 바뀌어 v2 기록을 그대로 옮길 수 없다(v2 기록은 더미였다). --reset으로 새로 만들고
v3 더미(scripts/generate_dummy_records.py)를 다시 넣는다.
"""
import argparse
import sys
from pathlib import Path

from sqlalchemy import delete, inspect, select, text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from psd.catalog import MAIN_SIDES, PART_TYPES, TAXONOMY  # noqa: E402
from psd.cycle import STATIONS  # noqa: E402
from psd.db import Base, engine, get_session  # noqa: E402
from psd.layout import ensure_station_parts, ensure_track  # noqa: E402
from psd.models import CycleResult, Door, Part, PartType, ReplacementPart, Station  # noqa: E402

# 모델에서 뺀 표(2026-11-23 정리). 순서대로 지운다 — daily_weather가 observatories를 참조한다
_DROP_TABLES = ["daily_weather", "observatories"]


def check_schema(engine) -> None:
    """v2 DB(정비기록 원인 코드·세부 부품 칸이 있는 표)면 --reset을 하라고 멈춘다. 칸 몇 개를 더하는 걸로는 못 옮긴다."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "replacement_records" in tables:
        cols = {c["name"] for c in inspector.get_columns("replacement_records")}
        if "cause" in cols or "symptom_detail" not in cols:
            sys.exit("v2 구조의 DB입니다. python scripts/init_db.py --reset 으로 새로 만든 뒤 v3 더미를 다시 넣으세요"
                     "(데이터셋_v3_설계.md 8장).")
    with engine.begin() as conn:
        for table in _DROP_TABLES:
            if table in tables:
                conn.execute(text(f"DROP TABLE {table}"))
                print(f"스키마 동기화: {table} 표 지움")


def seed(session) -> None:
    for s in STATIONS:
        session.merge(Station(id=s.order, name=s.name, open_date=s.open_date, near_water=s.near_water, in_service=s.in_service))

    codes = [p["code"] for p in PART_TYPES]
    for row in PART_TYPES:
        session.merge(PartType(code=row["code"], name=row["name"], unit=row["unit"], weather_sensitive=row["weather_sensitive"],
                               group_name=row["group"], formula=row["formula"]))
    session.flush()

    # 분류표에서 빠진 소분류: 기록이 없으면 부품·계산 결과와 함께 지운다. 기록이 있으면 멈춘다(기록을 지울 수는 없다)
    gone = [c for (c,) in session.execute(select(PartType.code)) if c not in codes]
    if gone:
        used = session.scalar(select(ReplacementPart.id).where(ReplacementPart.part_type_code.in_(gone)).limit(1))
        if used:
            sys.exit(f"분류표에 없는 소분류 {gone}의 기록이 DB에 있습니다. 분류표 파일을 확인하세요({TAXONOMY.source}).")
        session.execute(delete(Part).where(Part.part_type_code.in_(gone)))
        session.execute(delete(CycleResult).where(CycleResult.part_type_code.in_(gone)))
        session.execute(delete(PartType).where(PartType.code.in_(gone)))
        print(f"분류표에서 빠진 소분류를 지움: {gone}")

    for s in STATIONS:
        if not s.in_service:
            # 아직 PSD가 설치되지 않은 역(지금은 북울산뿐) — 문·부품을 만들지 않는다(있으면 지운다)
            session.execute(delete(Part).where(Part.station_id == s.order))
            session.execute(delete(Door).where(Door.station_id == s.order))
            continue
        install_date = s.psd_install_date or s.open_date
        sides = set(MAIN_SIDES) | {side for (side,) in session.execute(select(Door.side).where(Door.station_id == s.order).distinct())}
        for side in sorted(sides, key=lambda x: (len(x), x)):  # 본선 먼저, 기록으로 생긴 부본선도 새 소분류 부품을 받게
            ensure_track(session, s.order, side, install_date)
        ensure_station_parts(session, s.order, install_date)
        session.flush()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="표를 지우고 다시 만든다")
    args = ap.parse_args()

    if args.reset:
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:  # v2에만 있던 표·예전 표도 지운다
            for table in _DROP_TABLES:
                conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
    else:
        check_schema(engine)
    Base.metadata.create_all(engine)

    session = get_session()
    try:
        seed(session)
        session.commit()
        from sqlalchemy import func

        n_doors = session.scalar(select(func.count(Door.id)))
        n_parts = session.scalar(select(func.count(Part.id)))
        in_service = sum(s.in_service for s in STATIONS)
        print(f"적재 완료: 역 {len(STATIONS)}(운영 중 {in_service}), 문 {n_doors}, 소분류 {len(PART_TYPES)}"
              f"(문 단위 {sum(p['unit'] == 'door' for p in PART_TYPES)}), 부품 {n_parts} — 분류표: {TAXONOMY.source}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
