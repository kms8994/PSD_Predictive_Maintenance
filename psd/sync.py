"""엑셀(원본)과 DB 교체 기록 비교 (2026-11-01, 엑셀을 원본으로 두기로 함).

현장은 정비기록을 엑셀로 관리한다. 그래서 엑셀을 원본으로 보고, 업로드는 "DB에 덧붙이기"가 아니라
"엑셀에 맞추기"로 한다 — 같은 파일을 두 번 올려도 기록이 늘지 않고, 엑셀에서 고친 줄은 DB에서도 고쳐진다.

비교 범위는 파일에 나온 역들 × 파일의 가장 이른 날짜~가장 늦은 날짜다(api.py가 그 범위의 DB 기록만
골라 넘긴다). 누적 파일이면 전체와, 한 달·한 역 파일이면 그 달·그 역하고만 비교된다.

이 모듈은 DB 없이 비교만 한다(테스트하기 쉽게). 넣고 지우는 건 psd/api.py가 한다.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class RecordSig:
    """기록 한 건을 비교하는 기준. 입력자는 넣지 않는다(같은 교체를 누가 적었는지만 다르면 같은 기록)."""

    station_id: int
    door_id: int | None
    replaced_on: date
    reason: str
    parts: tuple[str, ...]  # 정렬된 부품 코드

    @property
    def slot(self) -> tuple[int, int | None, date]:
        return (self.station_id, self.door_id, self.replaced_on)


@dataclass
class Diff:
    new: list[RecordSig] = field(default_factory=list)  # 엑셀에만 있음 → 넣는다
    changed: list[tuple[int, RecordSig]] = field(default_factory=list)  # (DB 기록 id, 엑셀 값): 같은 역·문·날짜인데 사유·부품이 다름
    missing: list[int] = field(default_factory=list)  # DB에만 있음(범위 안) → 고르면 지운다
    db_duplicates: list[int] = field(default_factory=list)  # DB에 같은 기록이 두 번 이상 → 하나만 남기고 지운다
    same: int = 0
    file_duplicates: int = 0  # 엑셀 안에서 똑같은 줄이 또 나온 수(한 건만 넣는다)


def diff_records(file_sigs: list[RecordSig], db_records: list[tuple[int, RecordSig]]) -> Diff:
    """엑셀 행(file_sigs)과 범위 안의 DB 기록((id, sig))을 비교한다.

    같은 역·문·날짜에 "엑셀에만 있는 것"과 "DB에만 있는 것"이 함께 있으면 고친 것으로 보고 짝지어
    changed로 보낸다. 날짜를 고친 경우는 같은 기록인지 알 수 없어 new + missing으로 남는다.
    """
    d = Diff()
    unique: list[RecordSig] = []
    seen: set[RecordSig] = set()
    for s in file_sigs:
        if s in seen:
            d.file_duplicates += 1
        else:
            seen.add(s)
            unique.append(s)

    db_by_sig: dict[RecordSig, list[int]] = defaultdict(list)
    for rid, s in sorted(db_records, key=lambda x: x[0]):  # 먼저 들어간 기록을 남긴다
        db_by_sig[s].append(rid)

    new: list[RecordSig] = []
    for s in unique:
        ids = db_by_sig.get(s)
        if ids:
            ids.pop(0)
            d.same += 1
        else:
            new.append(s)

    missing_by_slot: dict[tuple, list[int]] = defaultdict(list)
    missing_sig: dict[int, RecordSig] = {}
    for s, ids in db_by_sig.items():
        for rid in ids:
            if s in seen:
                d.db_duplicates.append(rid)  # 엑셀에 한 번 있는 걸 DB는 여러 번 갖고 있음
            else:
                missing_by_slot[s.slot].append(rid)
                missing_sig[rid] = s

    for s in new:
        slot_ids = missing_by_slot.get(s.slot)
        if slot_ids:
            d.changed.append((slot_ids.pop(0), s))
        else:
            d.new.append(s)
    d.missing = sorted(rid for ids in missing_by_slot.values() for rid in ids)
    d.db_duplicates.sort()
    return d
