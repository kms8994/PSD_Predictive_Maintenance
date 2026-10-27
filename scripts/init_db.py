"""DB 표 만들기 + 초기 데이터 적재 (MVP_계획.md 3-2~3-4).

- 표: 역, 관측소, 문, 부품 종류, 부품, 교체 기록, 교체 부품, 일별 날씨, 계산 결과
- 초기 데이터: 역 24개(이 중 PSD 운영 중인 역만, 지금은 23개 — 북울산 제외), 관측소 2곳,
  방향별 문 16개(임시), 부품 종류 7개
- 부품 생성: 운영 중인 역마다 문 32개 × 6종(문 단위) + 종합제어반 릴레이 1개(역 단위)

    python scripts/init_db.py          # 비어 있으면 적재, 있으면 건너뜀
    python scripts/init_db.py --reset  # 표를 지우고 다시 만듦
"""
import argparse
import sys
from pathlib import Path

from sqlalchemy import delete, inspect, select, text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from psd.catalog import (  # noqa: E402
    CARS_PER_SIDE,
    DOOR_PART_CODES,
    DOORS_PER_SIDE,
    OBSERVATORIES,
    PART_TYPES,
    SIDES,
    STATION_PART_CODES,
)
from psd.cycle import OBS_CODE, STATIONS  # noqa: E402
from psd.db import Base, engine, get_session  # noqa: E402
from psd.models import Door, Observatory, Part, PartType, Station  # noqa: E402

# Alembic 같은 마이그레이션 도구가 없는 프로젝트다. Base.metadata.create_all()은 없는 표만
# 새로 만들고, 이미 있는 표에 컬럼을 추가해 주지는 않는다(SQLAlchemy 자체 제약) — 그래서
# near_water처럼 모델에 나중에 추가된 컬럼은 기존 DB에서 빠진 채로 남는다. 코드 리뷰로 찾은
# 버그: 실제로 이 세션에서 "Unknown column 'near_water'"로 한 번 재현됐고, 그때는 수동
# ALTER TABLE로 넘어갔다(스크립트엔 안 남음). 여기서 같은 걸 멱등적으로(이미 있으면 건너뜀)
# 자동으로 해, 다음에 또 같은 종류의 컬럼이 늘어도 수동 개입 없이 넘어가게 한다.
_NEW_COLUMNS = [
    ("stations", "near_water", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("stations", "in_service", "BOOLEAN NOT NULL DEFAULT TRUE"),
]


def sync_schema(engine) -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, column, ddl in _NEW_COLUMNS:
            if table not in existing_tables:
                continue  # 표 자체가 없으면 create_all()이 컬럼까지 포함해 새로 만들어 줌
            existing_columns = {c["name"] for c in inspector.get_columns(table)}
            if column not in existing_columns:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                print(f"스키마 동기화: {table}.{column} 컬럼 추가")


def seed(session) -> None:
    for row in OBSERVATORIES:
        session.merge(Observatory(**row))

    for s in STATIONS:
        session.merge(
            Station(
                id=s.order,
                name=s.name,
                open_date=s.open_date,
                observatory_id=OBS_CODE[s.obs],
                near_water=s.near_water,
                in_service=s.in_service,
            )
        )

    for row in PART_TYPES:
        session.merge(PartType(code=row["code"], name=row["name"], unit=row["unit"], weather_sensitive=row["weather_sensitive"]))

    session.flush()

    # 이미 있는 부품 id는 install_date를 새로 안 넣는다 — 코드 리뷰로 찾은 버그: 전에는
    # session.merge(Part(..., install_date=install_date))가 매번 역 기본 설치일을 명시적으로
    # 들고 있어서, 실제 교체 기록으로 install_date가 바뀐 부품도 init_db.py를 다시 돌리면
    # 조용히 역 기본값으로 되돌아갔다(has_record는 안 건드려서 "기록은 있는데 설치일은 틀린"
    # 상태가 됨). 새로 생기는 부품(목록에 종류·역이 늘었을 때)에만 기본 설치일을 준다.
    existing_part_ids = {pid for (pid,) in session.execute(select(Part.id))}

    door_id = 1
    part_id = 1
    for s in STATIONS:
        # 부품 설치일 = 역의 실제 PSD 사용개시일(psd_install_date, 2026-10-24 미팅 답변 Q3로
        # 확인된 역만 있음)이 있으면 그걸 쓰고, 없으면 기존처럼 철도 개통일(open_date)로 대신한다.
        install_date = s.psd_install_date or s.open_date

        door_count = len(SIDES) * DOORS_PER_SIDE
        part_count = door_count * len(DOOR_PART_CODES) + len(STATION_PART_CODES)
        if not s.in_service:
            # 아직 PSD가 설치되지 않은 역(지금은 북울산뿐) — 문·부품을 만들지 않는다. id는
            # 역 순서대로 그만큼 건너뛰어, 나중에 실제로 운영을 시작해도 다른 역의 id가 밀리지
            # 않게 한다. 코드 리뷰로 찾은 버그: 전에는 이 구분이 없어서 북울산도 다른 23개 역과
            # 똑같이 문 32개·부품 193개가 만들어져 있었다(open_date 임시값이 지나면서 "안전"으로
            # 표시됨) — 이미 만들어져 있던 그 행은 여기서 지운다.
            session.execute(delete(Part).where(Part.station_id == s.order))
            session.execute(delete(Door).where(Door.station_id == s.order))
            door_id += door_count
            part_id += part_count
            continue

        station_doors = []
        for side in SIDES:
            for n in range(1, DOORS_PER_SIDE + 1):
                car = (n - 1) // CARS_PER_SIDE + 1
                position = (n - 1) % CARS_PER_SIDE + 1
                door = Door(
                    id=door_id,
                    station_id=s.order,
                    side=side,
                    car=car,
                    position=position,
                    label=f"{SIDES[side]} {car}-{position}",
                )
                session.merge(door)
                station_doors.append(door)
                door_id += 1

        for door in station_doors:
            for code in DOOR_PART_CODES:
                kwargs = dict(id=part_id, part_type_code=code, station_id=s.order, door_id=door.id)
                if part_id not in existing_part_ids:
                    kwargs["install_date"] = install_date
                session.merge(Part(**kwargs))
                part_id += 1

        for code in STATION_PART_CODES:
            kwargs = dict(id=part_id, part_type_code=code, station_id=s.order, door_id=None)
            if part_id not in existing_part_ids:
                kwargs["install_date"] = install_date
            session.merge(Part(**kwargs))
            part_id += 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="표를 지우고 다시 만든다")
    args = ap.parse_args()

    if args.reset:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    sync_schema(engine)

    session = get_session()
    try:
        # seed()는 session.merge()로 역·관측소·부품 종류·문·부품만 올리거나 갱신한다(교체
        # 기록 표는 건드리지 않고, 이미 있는 부품의 install_date도 더는 덮어쓰지 않는다 —
        # 위 existing_part_ids 주석 참고). 이미 있어도 다시 실행해 안전하다 — STATIONS나
        # PART_TYPES에 새 항목을 추가했을 때 조용히 건너뛰지 않도록 항상 seed한다.
        seed(session)
        session.commit()
        in_service = [s for s in STATIONS if s.in_service]
        door_count = len(SIDES) * DOORS_PER_SIDE
        part_count = door_count * len(DOOR_PART_CODES) + len(STATION_PART_CODES)
        print(
            f"적재 완료: 관측소 {len(OBSERVATORIES)}, 역 {len(STATIONS)}(운영 중 {len(in_service)}), "
            f"문 {len(in_service) * door_count}, "
            f"부품 종류 {len(PART_TYPES)}, "
            f"부품 {len(in_service) * part_count}"
        )
    finally:
        session.close()


if __name__ == "__main__":
    main()
