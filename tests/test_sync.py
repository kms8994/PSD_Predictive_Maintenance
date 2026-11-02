"""psd/sync.py 검증: 엑셀(원본)과 DB 기록 비교. DB 없이 돈다."""
from datetime import date

from psd.sync import RecordSig, diff_records


def sig(day: int, door: int | None = 1, reason: str = "fail", parts=("IFM",), station: int = 1) -> RecordSig:
    return RecordSig(station, door, date(2026, 5, day), reason, tuple(parts))


def test_same_file_twice_changes_nothing():
    rows = [sig(1), sig(2), sig(3, door=None, parts=("PSC",))]
    d = diff_records(rows, [(i + 10, s) for i, s in enumerate(rows)])
    assert (d.new, d.changed, d.missing, d.db_duplicates, d.same) == ([], [], [], [], 3)


def test_new_rows_are_added_and_rows_only_in_db_are_missing():
    d = diff_records([sig(1), sig(2)], [(10, sig(1)), (11, sig(9))])
    assert d.new == [sig(2)]
    assert d.missing == [11]
    assert d.same == 1


def test_same_door_and_day_with_different_reason_is_a_change():
    d = diff_records([sig(1, reason="prev")], [(10, sig(1, reason="fail"))])
    assert d.changed == [(10, sig(1, reason="prev"))]
    assert d.new == [] and d.missing == []


def test_changed_date_is_new_plus_missing():
    """날짜를 고치면 같은 기록인지 알 수 없어 짝짓지 않는다."""
    d = diff_records([sig(2)], [(10, sig(1))])
    assert d.new == [sig(2)] and d.missing == [10] and d.changed == []


def test_duplicates_in_file_are_counted_once():
    d = diff_records([sig(1), sig(1), sig(2)], [])
    assert d.new == [sig(1), sig(2)]
    assert d.file_duplicates == 1


def test_duplicates_in_db_keep_the_oldest():
    d = diff_records([sig(1)], [(12, sig(1)), (10, sig(1)), (11, sig(1))])
    assert d.same == 1
    assert d.db_duplicates == [11, 12]
    assert d.missing == []


def test_input_by_is_not_part_of_the_match():
    """입력자는 비교에 안 쓴다 — RecordSig에 입력자 칸이 없다."""
    assert "created_by" not in RecordSig.__dataclass_fields__
