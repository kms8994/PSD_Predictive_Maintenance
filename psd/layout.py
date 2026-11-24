"""문·부품 배치 만들기 (2026-11-24 v3, 데이터셋_v3_설계.md 4장).

선로마다 문 DOORS_PER_SIDE개를 두고, 문마다 문 단위 소분류 부품을, 역마다 역 단위 소분류 부품을 하나씩 둔다.
scripts/init_db.py는 상·하 본선을, psd/api.py는 기록에 처음 나온 부본선을 이걸로 만든다.
이미 있는 문·부품은 그대로 두고(설치일을 덮어쓰지 않는다) 빠진 것만 더한다.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from psd.catalog import CARS_PER_SIDE, DOOR_PART_CODES, DOORS_PER_SIDE, SIDES, STATION_PART_CODES
from psd.models import Door, Part


def door_label(side: str, car: int, position: int) -> str:
    return f"{SIDES[side]} {car}-{position}"


def ensure_track(session: Session, station_id: int, side: str, install_date: date) -> list[Door]:
    """역의 선로 하나에 문과 문 단위 부품을 만든다(없는 것만). 그 선로의 문 목록을 돌려준다."""
    existing = {(d.car, d.position): d for d in session.execute(
        select(Door).where(Door.station_id == station_id, Door.side == side)).scalars()}
    doors = []
    for n in range(1, DOORS_PER_SIDE + 1):
        car, position = (n - 1) // CARS_PER_SIDE + 1, (n - 1) % CARS_PER_SIDE + 1
        door = existing.get((car, position))
        if door is None:
            door = Door(station_id=station_id, side=side, car=car, position=position, label=door_label(side, car, position))
            session.add(door)
        doors.append(door)
    session.flush()
    have = {(p.door_id, p.part_type_code) for p in session.execute(
        select(Part).where(Part.door_id.in_([d.id for d in doors]))).scalars()}
    for d in doors:
        for code in DOOR_PART_CODES:
            if (d.id, code) not in have:
                session.add(Part(part_type_code=code, station_id=station_id, door_id=d.id, install_date=install_date))
    return doors


def ensure_station_parts(session: Session, station_id: int, install_date: date) -> None:
    """역 단위 소분류 부품을 만든다(없는 것만)."""
    have = {code for (code,) in session.execute(
        select(Part.part_type_code).where(Part.station_id == station_id, Part.door_id.is_(None)))}
    for code in STATION_PART_CODES:
        if code not in have:
            session.add(Part(part_type_code=code, station_id=station_id, door_id=None, install_date=install_date))
