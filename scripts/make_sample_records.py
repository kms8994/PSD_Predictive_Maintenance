"""작은 정비 기록 엑셀 샘플을 만든다 — 고장·교체 + 정기 점검(이상 없음) (2026-10-31).

현장에서 몇 달치 기록을 모아 한 번에 올린다고 가정한 크기다.

- 교체 기록(기본 30건): 운영 중인 역 여러 곳에 흩어 넣고, 사유는 고장·예방 교체·함께 교체를 섞는다.
  고장은 날씨 공식 주기가 짧은 역·부품일수록 자주 뽑힌다(와이블 형상 2에서 같은 나이의 고장 위험은
  주기의 제곱에 반비례).
- 정기 점검(기본 2개 역 × 2회): 코레일 담당자 설명대로 일정 주기마다 점검해 문제가 없었던 것도
  기록한다고 보고, 그 역의 문 32개와 종합제어반을 하루에 점검해 "점검(이상 없음)" 한 줄씩 넣는다
  (부품 칸은 "전체"). 그 날 그 문에 교체 기록이 있으면 점검 줄은 뺀다.
  "확인 필요"(기록 없이 예정일이 지난 부품)가 많은 역을 먼저 고른다 — 올리면 그 부품들이 바꿨는지 모르는
  상태에서 "점검했더니 아직 그 부품 그대로"인 교체 대상(위험)으로 바뀌는 걸 볼 수 있다.

- 비교 범위 안의 기존 기록: 업로드는 "파일에 나온 역 × 파일의 가장 이른~늦은 날짜" 안에서 엑셀에 맞춘다
  (psd/sync.py). 현장의 몇 달치 파일엔 그 기간 기록이 다 있을 테니, DB에 이미 있는 그 범위의 기록(초기 데이터)도
  그대로 넣는다 — 안 넣으면 올릴 때 그 기록들이 전부 "엑셀에 없는 기록"으로 잡힌다(2026-11-02).

업로드하면 여러 역·부품에 기록이 조금씩 들어가는 모습을 보여 주는 용도다. 화면의 "정비기록 시연" 탭이
이 파일(data/dummy/정비기록_샘플_<N>건.xlsx, 이름은 psd/api.py의 SAMPLE_RECORDS_PATH)을 입력으로 쓴다.
교체 기록은 그 문·부품의 마지막 기록보다 뒤 날짜만 쓴다 — 기존 기록 사이에 끼면 설치일 계산이 엉킨다.

    python scripts/make_sample_records.py                 # → data/dummy/정비기록_샘플_<N>건.xlsx
    python scripts/make_sample_records.py --count 50 --inspect-stations 3 --seed 3
"""
import argparse
import random
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import joinedload  # noqa: E402

from psd.catalog import DOOR_PART_CODES, NAME_TO_CODE, SIDES  # noqa: E402
from psd.db import get_session  # noqa: E402
from psd.excel_io import REASON_CODE_TO_LABEL, parse_upload, rows_to_excel_bytes  # noqa: E402
from psd.models import CycleResult, Door, ReplacementPart, ReplacementRecord, Station  # noqa: E402
from psd.status import all_unit_statuses  # noqa: E402

CODE_TO_NAME = {code: name for name, code in NAME_TO_CODE.items()}
TEAMS = ["정비1팀", "정비2팀"]
WITH_PROB = 0.2   # 고장 한 건에 같은 문 다른 부품을 "함께 교체"로 붙일 확률
PREV_SHARE = 0.2  # 예방 교체 행의 비율
PSC_WEIGHT = 0.3  # 종합제어반 릴레이(역마다 1개)는 문 부품(역마다 32개)보다 덜 뽑히게
INSPECT_MONTHS = [6, 9]  # 분기 정기 점검 달(2026년)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=30, help="교체 기록 행 수(대략, 함께 교체가 붙어 1~2건 넘칠 수 있음)")
    ap.add_argument("--inspect-stations", type=int, default=2, help="정기 점검 기록을 넣을 역 수(0이면 점검 없음)")
    ap.add_argument("--start", default="2026-04-01", help="교체일을 뽑을 구간 시작")
    ap.add_argument("--end", default="2026-10-15", help="교체일을 뽑을 구간 끝")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--out", help="안 주면 data/dummy/정비기록_샘플_<N>건.xlsx")
    args = ap.parse_args()

    random.seed(args.seed)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    session = get_session()
    try:
        stations = {s.id: s for s in session.execute(select(Station).where(Station.in_service)).scalars()}
        doors = {}
        for d in session.execute(select(Door).where(Door.station_id.in_(stations))).scalars():
            doors.setdefault(d.station_id, []).append(d)
        base = {(c.station_id, c.part_type_code): c.base_years for c in session.execute(select(CycleResult)).scalars()}
        last = {
            (st, door, code): day for st, door, code, day in session.execute(
                select(ReplacementRecord.station_id, ReplacementRecord.door_id, ReplacementPart.part_type_code, func.max(ReplacementRecord.replaced_on))
                .join(ReplacementPart, ReplacementPart.record_id == ReplacementRecord.id)
                .group_by(ReplacementRecord.station_id, ReplacementRecord.door_id, ReplacementPart.part_type_code)
            )
        }

        # ---- 교체 기록 ----
        # 고장 후보: (역, 부품) — 주기가 짧을수록 무겁게
        fail_keys = [(st, code) for st in stations for code in DOOR_PART_CODES + ["PSC"]]
        fail_w = [(PSC_WEIGHT if code == "PSC" else 1.0) / base.get((st, code), 10.0) ** 2 for st, code in fail_keys]

        used, rows = set(), []

        def pick_date(st, door_id, codes):
            lo = max([start] + [last[(st, door_id, c)] + timedelta(days=1) for c in codes if (st, door_id, c) in last])
            return None if lo > end else lo + timedelta(days=random.randint(0, (end - lo).days))

        def add(st, door, codes, day, reason):
            used.update((st, door.id if door else None, c) for c in codes)
            rows.append({"역": stations[st].name, "방향": SIDES[door.side] if door else "", "문번호": f"{door.car}-{door.position}" if door else "",
                         "교체일": day.isoformat(), "사유": reason, "부품": ",".join(CODE_TO_NAME[c] for c in codes), "입력자": random.choice(TEAMS)})

        tries = 0
        while len(rows) < args.count and tries < args.count * 50:
            tries += 1
            if random.random() < PREV_SHARE:
                st = random.choice(list(stations))
                door = random.choice(doors[st])
                codes = sorted(random.sample(DOOR_PART_CODES, random.choice([1, 2])), key=DOOR_PART_CODES.index)
                reason = "예방 교체"
            else:
                st, code = random.choices(fail_keys, fail_w)[0]
                door = None if code == "PSC" else random.choice(doors[st])
                codes = [code]
                reason = "고장"
            door_id = door.id if door else None
            if any((st, door_id, c) in used for c in codes):
                continue
            day = pick_date(st, door_id, codes)
            if day is None:
                continue
            add(st, door, codes, day, reason)
            if reason == "고장" and door and random.random() < WITH_PROB:
                rest = [c for c in DOOR_PART_CODES if c != codes[0] and (st, door_id, c) not in used and day > last.get((st, door_id, c), date.min)]
                if rest:
                    add(st, door, [random.choice(rest)], day, "함께 교체")
        n_replace = len(rows)

        # ---- 정기 점검(이상 없음) ----
        unchecked = Counter(u.station_id for u in all_unit_statuses(session) if u.status == "확인_필요" and u.station_id in stations)
        order = sorted(stations, key=lambda st: (-unchecked[st], random.random()))
        inspected = order[:args.inspect_stations]
        busy = {(r["역"], r["방향"], r["문번호"], r["교체일"]) for r in rows}  # 같은 날 교체한 문은 점검 줄을 안 넣는다
        for st in inspected:
            for month in INSPECT_MONTHS:
                day = date(start.year, month, random.randint(5, 25))
                if not (start <= day <= end):
                    continue
                team = random.choice(TEAMS)
                for door in sorted(doors[st], key=lambda d: (d.side, d.car, d.position)):
                    side, no = SIDES[door.side], f"{door.car}-{door.position}"
                    if (stations[st].name, side, no, day.isoformat()) in busy:
                        continue
                    rows.append({"역": stations[st].name, "방향": side, "문번호": no, "교체일": day.isoformat(),
                                 "사유": "점검(이상 없음)", "부품": "전체", "입력자": team})
                rows.append({"역": stations[st].name, "방향": "", "문번호": "", "교체일": day.isoformat(),
                             "사유": "점검(이상 없음)", "부품": "전체", "입력자": team})

        # ---- 비교 범위 안의 기존 기록(그대로 둘 것) ----
        slot = lambda r: (r["역"], r["방향"], r["문번호"], r["교체일"])  # noqa: E731
        names = {r["역"] for r in rows}
        lo, hi = min(r["교체일"] for r in rows), max(r["교체일"] for r in rows)
        existing = session.execute(
            select(ReplacementRecord).options(joinedload(ReplacementRecord.door), joinedload(ReplacementRecord.parts))
            .where(ReplacementRecord.station_id.in_([st for st in stations if stations[st].name in names]),
                   ReplacementRecord.replaced_on.between(date.fromisoformat(lo), date.fromisoformat(hi)))
        ).unique().scalars().all()
        old = [{"역": stations[r.station_id].name, "방향": SIDES[r.door.side] if r.door else "", "문번호": f"{r.door.car}-{r.door.position}" if r.door else "",
                "교체일": r.replaced_on.isoformat(), "사유": REASON_CODE_TO_LABEL[r.reason],
                "부품": ",".join(CODE_TO_NAME[c] for c in sorted(p.part_type_code for p in r.parts)), "입력자": r.created_by or ""}
               for r in existing]
        taken = {slot(r) for r in old}  # 같은 날 기존 기록이 있는 문은 점검 줄을 뺀다(고친 기록으로 짝지어지지 않게)
        rows = [r for r in rows if not (r["사유"] == "점검(이상 없음)" and slot(r) in taken)]
        n_new, new_st = len(rows), len({r["역"] for r in rows if r["사유"] != "점검(이상 없음)"})
        rows += old
        rows.sort(key=lambda r: (r["교체일"], r["역"], r["방향"], r["문번호"]))

        content = rows_to_excel_bytes(rows)
        parse_upload(content)  # 업로드 화면과 같은 규칙으로 읽히는지 확인(틀리면 여기서 에러)

        reasons = Counter(r["사유"] for r in rows)
        print(f"{len(rows)}건 · " + " · ".join(f"{k} {v}" for k, v in reasons.most_common()))
        print(f"교체 기록 {n_replace}건: 역 {new_st}곳")
        print(f"새 기록 {n_new}건 + 범위 안 기존 기록 {len(old)}건({lo} ~ {hi}, 역 {len(names)}곳)")
        print("점검한 역:", ", ".join(f"{stations[st].name}(확인 필요 {unchecked[st]}개)" for st in inspected) or "없음")

        out = Path(args.out) if args.out else ROOT / "data" / "dummy" / f"정비기록_샘플_{len(rows)}건.xlsx"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(content)
        print(f"저장: {out}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
