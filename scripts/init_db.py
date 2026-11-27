"""DB 표 만들기 + 고정 데이터 맞추기 (MVP_계획.md 3-2~3-4, 2026-11-24 v3·역 목록 파일).

- 표: 역, 문, 부품 종류(= 장애부위 소분류), 부품, 정비 기록, 기록의 소분류, 업로드 이력, 계산 결과, AI 모델 이력
- 역: 고정 파일 data/stations/동해선_역목록.xlsx(psd/stations.py). 운영 중인 역마다 상·하 본선 문 16개(임시)와 부품
- 소분류: 분류표 파일(psd/taxonomy.py)

    python scripts/init_db.py --check  # 역 목록·분류표가 DB와 무엇이 다른지 보기만 한다(아무것도 안 바꿈)
    python scripts/init_db.py          # 맞춘다(다시 돌려도 안전)
    python scripts/init_db.py --reset  # 표를 지우고 다시 만든다 — v2 DB(7종 부품)에서 옮길 때만

역 목록을 바꿨을 때(데이터셋은 그대로):
- 새 역(새 ID): 역을 더하고, 운영이면 문·부품을 만든다. 그 뒤 scripts/compute_cycles.py로 주기를 계산한다.
- 이름·순서·개통일·바다 인접이 바뀜: 역 행만 고친다(기록은 ID로 묶여 그대로). 예전 이름은 파일의 '옛 이름'에 적어 둔다.
- PSD 사용개시일이 바뀜: 교체 기록이 없는 부품의 설치일을 새 날로 옮긴다(교체한 부품은 그 교체일 그대로).
- 개통 전 → 운영: 문·부품을 만든다. 운영 → 개통 전: 기록이 없을 때만 문·부품을 지운다(있으면 멈춘다).
- 파일에서 빠진 역: 기록이 없을 때만 지운다(있으면 멈춘다 — 지우지 말고 '개통 전'으로 두라고 알린다).
- AWS 지점이 바뀜: scripts/compute_cycles.py를 다시 돌리라고 알린다.
"""
import argparse
import sys
from pathlib import Path

from sqlalchemy import delete, func, inspect, select, text, update

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from psd.catalog import MAIN_SIDES, PART_TYPES, TAXONOMY  # noqa: E402
from psd.db import Base, engine, get_session  # noqa: E402
from psd.layout import ensure_station_parts, ensure_track  # noqa: E402
from psd.models import CycleResult, Door, Part, PartType, ReplacementPart, ReplacementRecord, Station  # noqa: E402
from psd.stations import STATION_FILE, STATIONS  # noqa: E402

_DROP_TABLES = ["daily_weather", "observatories"]  # 2026-11-23 정리한 예전 표
_NEW_COLUMNS = [("stations", "seq", "INT NOT NULL DEFAULT 0"),  # 2026-11-24 역 목록 파일(노선 순서)
                ("upload_batches", "source", "VARCHAR(20) NULL")]  # 2026-11-27 연동 파일 감지로 반영한 업로드(psd/link.py)
_DROP_COLUMNS = [("stations", "coast_km"), ("stations", "daily_riders"), ("stations", "env_note")]  # 역 환경 파일로 옮김


def check_schema(engine, apply: bool) -> list[str]:
    """v2 DB면 멈추고, 칸 더하기·지우기가 필요하면 (apply일 때) 한다. 한 일(할 일) 목록."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "replacement_records" in tables:
        cols = {c["name"] for c in inspector.get_columns("replacement_records")}
        if "cause" in cols or "symptom_detail" not in cols:
            sys.exit("v2 구조의 DB입니다. python scripts/init_db.py --reset 으로 새로 만든 뒤 v3 더미를 다시 넣으세요"
                     "(데이터셋_v3_설계.md 8장).")
    todo = []
    cols = {t: {c["name"] for c in inspector.get_columns(t)} for t in tables}
    for table, column, ddl in _NEW_COLUMNS:
        if table in tables and column not in cols[table]:
            todo.append((f"ALTER TABLE {table} ADD COLUMN {column} {ddl}", f"{table}.{column} 칸 추가"))
    for table, column in _DROP_COLUMNS:
        if table in tables and column in cols[table]:
            todo.append((f"ALTER TABLE {table} DROP COLUMN {column}", f"{table}.{column} 칸 지움"))
    for table in _DROP_TABLES:
        if table in tables:
            todo.append((f"DROP TABLE {table}", f"{table} 표 지움"))
    if apply and todo:
        with engine.begin() as conn:
            for sql, _msg in todo:
                conn.execute(text(sql))
    return [f"스키마: {msg}" for _sql, msg in todo]


def station_changes(session) -> tuple[list[str], list[str], bool]:
    """역 목록 파일과 DB의 차이: (바뀌는 것, 막히는 것, 주기 계산을 다시 해야 하는지)."""
    db = {s.id: s for s in session.execute(select(Station)).scalars()}
    has_records = {sid for (sid,) in session.execute(select(ReplacementRecord.station_id).distinct())}
    door_stations = {sid for (sid,) in session.execute(select(Door.station_id).distinct())}
    file_ids = {s.id for s in STATIONS}
    changes, blocked, recompute = [], [], False
    for s in STATIONS:
        old = db.get(s.id)
        if old is None:
            changes.append(f"새 역: {s.name}(ID {s.id}, 순서 {s.seq}, {'운영' if s.in_service else '개통 전'})")
            recompute = True
            continue
        if old.name != s.name:
            note = "" if old.name in s.aliases else f" — 예전 이름 {old.name!r}를 파일의 '옛 이름'에 적으면 예전 이름의 기록도 읽힘"
            changes.append(f"이름: ID {s.id} {old.name} → {s.name}{note}")
        if old.seq != s.seq:
            changes.append(f"순서: {s.name} {old.seq} → {s.seq}")
        if old.open_date != s.open_date:
            changes.append(f"개통일: {s.name} {old.open_date} → {s.open_date}")
        if old.near_water != s.near_water:
            changes.append(f"바다·하천 인접: {s.name} {'예' if s.near_water else '아니오'}")
        if old.in_service != s.in_service:
            if s.in_service:
                changes.append(f"운영 시작: {s.name} — 문·부품을 만듦")
                recompute = True
            elif s.id in has_records:
                blocked.append(f"{s.name}을(를) '개통 전'으로 바꿀 수 없습니다 — 정비기록이 있습니다")
            else:
                changes.append(f"개통 전으로: {s.name} — 문·부품을 지움")
        elif s.in_service and s.id not in door_stations:
            changes.append(f"문·부품 만들기: {s.name}")
    for sid, old in db.items():
        if sid not in file_ids:
            if sid in has_records:
                blocked.append(f"{old.name}(ID {sid})이(가) 역 목록 파일에 없지만 정비기록이 있어 지울 수 없습니다 — 파일에 다시 넣고 '개통 전'으로 두세요")
            else:
                changes.append(f"역 지움: {old.name}(ID {sid}) — 파일에 없고 기록도 없음")
    return changes, blocked, recompute


def psd_start_changes(session) -> list[tuple[int, str, object]]:
    """PSD 사용개시일이 바뀐 역: 교체 기록이 없는 부품의 설치일이 파일 값과 다른 역 (역 id, 이름, 새 날짜)."""
    out = []
    for s in STATIONS:
        if not s.in_service:
            continue
        wrong = session.scalar(select(func.count(Part.id)).where(Part.station_id == s.id, Part.has_record.is_(False),
                                                                 Part.install_date != s.psd_start))
        if wrong:
            out.append((s.id, s.name, s.psd_start))
    return out


def seed(session) -> None:
    file_ids = {s.id for s in STATIONS}
    for sid in [sid for (sid,) in session.execute(select(Station.id)) if sid not in file_ids]:
        session.execute(delete(Part).where(Part.station_id == sid))
        session.execute(delete(Door).where(Door.station_id == sid))
        session.execute(delete(CycleResult).where(CycleResult.station_id == sid))
        session.execute(delete(Station).where(Station.id == sid))
    for s in STATIONS:
        session.merge(Station(id=s.id, seq=s.seq, name=s.name, open_date=s.open_date, near_water=s.near_water, in_service=s.in_service))

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

    for sid, name, start in psd_start_changes(session):
        n = session.execute(update(Part).where(Part.station_id == sid, Part.has_record.is_(False)).values(install_date=start)).rowcount
        print(f"PSD 사용개시일: {name} 교체 기록 없는 부품 {n}개의 설치일을 {start}로")

    for s in STATIONS:
        if not s.in_service:
            session.execute(delete(Part).where(Part.station_id == s.id))
            session.execute(delete(Door).where(Door.station_id == s.id))
            continue
        sides = set(MAIN_SIDES) | {side for (side,) in session.execute(select(Door.side).where(Door.station_id == s.id).distinct())}
        for side in sorted(sides, key=lambda x: (len(x), x)):  # 본선 먼저, 기록으로 생긴 부본선도 새 소분류 부품을 받게
            ensure_track(session, s.id, side, s.psd_start)
        ensure_station_parts(session, s.id, s.psd_start)
        session.flush()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="DB와 무엇이 다른지 보기만 한다")
    ap.add_argument("--reset", action="store_true", help="표를 지우고 다시 만든다")
    args = ap.parse_args()

    print(f"역 목록: {STATION_FILE.name} (역 {len(STATIONS)}개, 운영 {sum(s.in_service for s in STATIONS)}) · 분류표: {TAXONOMY.source}")
    if args.reset:
        if args.check:
            sys.exit("--check와 --reset은 같이 쓸 수 없습니다")
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:  # v2에만 있던 표·예전 표도 지운다
            for table in _DROP_TABLES:
                conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        schema = []
    else:
        schema = check_schema(engine, apply=not args.check)
    if not args.check:
        Base.metadata.create_all(engine)

    session = get_session()
    try:
        if args.check:
            if "stations" not in inspect(engine).get_table_names():
                print("DB에 표가 없습니다 — python scripts/init_db.py 로 만든다")
                return
            changes, blocked, recompute = station_changes(session) if not any("seq" in x for x in schema) else ([], [], False)
            starts = psd_start_changes(session)
            lines = schema + changes + [f"PSD 사용개시일: {n} 교체 기록 없는 부품 설치일 → {d}" for _i, n, d in starts]
            print("\n".join(f"- {x}" for x in lines) if lines else "DB가 파일과 같습니다.")
            for b in blocked:
                print(f"! 막힘: {b}")
            if recompute:
                print("반영한 뒤 python scripts/compute_cycles.py 도 돌려야 합니다(새 역·운영 시작)")
            return

        changes, blocked, recompute = station_changes(session)
        if blocked:
            sys.exit("반영할 수 없습니다:\n! " + "\n! ".join(blocked))
        for x in schema + changes:
            print(f"- {x}")
        seed(session)
        session.commit()
        n_doors = session.scalar(select(func.count(Door.id)))
        n_parts = session.scalar(select(func.count(Part.id)))
        print(f"적재 완료: 역 {len(STATIONS)}(운영 중 {sum(s.in_service for s in STATIONS)}), 문 {n_doors}, 소분류 {len(PART_TYPES)}"
              f"(문 단위 {sum(p['unit'] == 'door' for p in PART_TYPES)}), 부품 {n_parts}")
        missing = session.scalar(select(func.count()).select_from(Part).where(
            ~select(CycleResult.station_id).where(CycleResult.station_id == Part.station_id,
                                                  CycleResult.part_type_code == Part.part_type_code).exists()))
        if recompute or missing:
            print("다음: python scripts/compute_cycles.py — 주기 계산이 없는 역·소분류가 있습니다")
    finally:
        session.close()


if __name__ == "__main__":
    main()
