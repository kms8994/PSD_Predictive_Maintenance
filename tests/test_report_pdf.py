"""psd/report_pdf.py 검증: DB 없이 가짜 상태로 PDF를 만든다.

실제 DB는 지금 전부 "안전"이라 조치 필요 표(3번)와 색칠 칸이 안 나온다 — 그 경로를 여기서 확인한다.
"""
import io
from datetime import date, timedelta
from types import SimpleNamespace

from pypdf import PdfReader

from psd.report_pdf import DOOR_CODES, DoorInfo, RecordInfo, build_station_pdf
from psd.status import UnitStatus, judge_status

TODAY = date(2026, 10, 27)


def _unit(part_id, door_id, code, days_left, has_record=False):
    due = TODAY + timedelta(days=days_left)
    return UnitStatus(
        part_id=part_id, station_id=1, door_id=door_id, part_type_code=code, install_date=date(2022, 1, 1),
        has_record=has_record, cycle=SimpleNamespace(base_years=7.0), due=due, days_left=days_left,
        status=judge_status(days_left, has_record),
    )


def _build(statuses, doors, records=()):
    return build_station_pdf(
        station_id=1, station_name="부전", psd_install_date=date(2022, 1, 1), statuses=statuses,
        doors=doors, records=list(records), record_total=len(records), today=TODAY,
    )


def test_action_table_lists_warning_parts_most_urgent_first():
    doors = [DoorInfo(1, "U", 1, 1, "부전 방면 1-1"), DoorInfo(2, "D", 1, 1, "북울산 방면 1-1")]
    statuses = [_unit(i, 1, code, 2000) for i, code in enumerate(DOOR_CODES)]
    statuses += [_unit(100 + i, 2, code, 2000) for i, code in enumerate(DOOR_CODES)]
    statuses[0] = _unit(0, 1, DOOR_CODES[0], 300)  # 주의
    statuses[7] = _unit(107, 2, DOOR_CODES[1], 30)  # 위험
    statuses.append(_unit(999, None, "PSC", -40))  # 확인 필요(기록 없이 지남)
    records = [RecordInfo(date(2026, 10, 1), "부전 방면 1-1", ["DCM", "IFM"], "fail", "정비1팀")]

    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(_build(statuses, doors, records))).pages)

    assert "조치가 필요한 부품이 없습니다" not in text
    first, second, third = text.index("위험"), text.index("확인 필요"), text.index("주의")
    assert first < second < third  # 표 머리(상태 요약)에서 위험·확인 필요·주의 순서
    assert "40일 지남" in text
    assert "DCM, IFM 릴레이" in text


def test_no_warning_parts_says_so_and_shows_soonest():
    doors = [DoorInfo(1, "U", 1, 1, "부전 방면 1-1")]
    statuses = [_unit(i, 1, code, 1000 + i) for i, code in enumerate(DOOR_CODES)] + [_unit(99, None, "PSC", 3000)]

    text = PdfReader(io.BytesIO(_build(statuses, doors))).pages[0].extract_text()

    assert "조치가 필요한 부품이 없습니다" in text
    assert "교체 기록 없음" in text
