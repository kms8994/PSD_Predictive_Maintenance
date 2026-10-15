"""DB 표 만들기 + 초기 데이터 적재 (MVP_계획.md 3-2~3-4).

- 표: 역, 관측소, 문, 부품 종류, 부품, 교체 기록, 교체 부품, 일별 날씨, 계산 결과
- 초기 데이터: 역 24개, 관측소 2곳, 방향별 문 16개(임시), 부품 종류 7개
- 부품 생성: 역마다 문 32개 × 6종(문 단위) + 종합제어반 릴레이 1개(역 단위)

    python scripts/init_db.py          # 비어 있으면 적재, 있으면 건너뜀
    python scripts/init_db.py --reset  # 표를 지우고 다시 만듦
"""
import argparse
import sys
from pathlib import Path

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


def seed(session) -> None:
    for row in OBSERVATORIES:
        session.merge(Observatory(**row))

    for s in STATIONS:
        session.merge(
            Station(id=s.order, name=s.name, open_date=s.open_date, observatory_id=OBS_CODE[s.obs])
        )

    for row in PART_TYPES:
        session.merge(PartType(code=row["code"], name=row["name"], unit=row["unit"], weather_sensitive=row["weather_sensitive"]))

    session.flush()

    door_id = 1
    part_id = 1
    for s in STATIONS:
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
                session.merge(Part(id=part_id, part_type_code=code, station_id=s.order, door_id=door.id, install_date=s.open_date))
                part_id += 1

        for code in STATION_PART_CODES:
            session.merge(Part(id=part_id, part_type_code=code, station_id=s.order, door_id=None, install_date=s.open_date))
            part_id += 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="표를 지우고 다시 만든다")
    args = ap.parse_args()

    if args.reset:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    session = get_session()
    try:
        # seed()는 session.merge()로 역·관측소·부품 종류·문·부품만 올리거나 갱신한다(교체
        # 기록 표는 건드리지 않음). 이미 있어도 다시 실행해 안전하다 — STATIONS나
        # PART_TYPES에 새 항목을 추가했을 때 조용히 건너뛰지 않도록 항상 seed한다.
        seed(session)
        session.commit()
        print(
            f"적재 완료: 관측소 {len(OBSERVATORIES)}, 역 {len(STATIONS)}, "
            f"문 {len(STATIONS) * len(SIDES) * DOORS_PER_SIDE}, "
            f"부품 종류 {len(PART_TYPES)}, "
            f"부품 {len(STATIONS) * (len(SIDES) * DOORS_PER_SIDE * len(DOOR_PART_CODES) + len(STATION_PART_CODES))}"
        )
    finally:
        session.close()


if __name__ == "__main__":
    main()
