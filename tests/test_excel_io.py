"""psd/excel_io.py 검증: 엑셀 업로드 파싱 (2026-10-25, 고장 데이터 엑셀 입력)."""
from datetime import date

import pytest

from psd.excel_io import COLUMNS, ExcelFormatError, parse_upload, rows_to_excel_bytes


def _xlsx(rows: list[dict]) -> bytes:
    return rows_to_excel_bytes(rows)


def test_parse_upload_reads_door_level_row():
    content = _xlsx([
        {"역": "부전", "방향": "부전 방면", "문번호": "1-1", "교체일": "2026-01-15",
         "사유": "고장", "부품": "DCM,IFM 릴레이", "입력자": "홍길동"},
    ])
    rows = parse_upload(content)
    assert len(rows) == 1
    r = rows[0]
    assert r.station_name == "부전"
    assert r.side_label == "부전 방면"
    assert r.door_no == "1-1"
    assert r.replaced_on == date(2026, 1, 15)
    assert r.reason_code == "fail"
    assert r.part_names == ["DCM", "IFM 릴레이"]
    assert r.created_by == "홍길동"


def test_parse_upload_reads_station_level_row_with_blank_side_door():
    content = _xlsx([
        {"역": "부전", "방향": "", "문번호": "", "교체일": "2026-01-15",
         "사유": "예방 교체", "부품": "종합제어반 릴레이", "입력자": ""},
    ])
    rows = parse_upload(content)
    assert rows[0].side_label is None
    assert rows[0].door_no is None
    assert rows[0].reason_code == "prev"
    assert rows[0].created_by is None


def test_parse_upload_rejects_unknown_part_name():
    content = _xlsx([
        {"역": "부전", "방향": "", "문번호": "", "교체일": "2026-01-15",
         "사유": "고장", "부품": "없는부품", "입력자": ""},
    ])
    with pytest.raises(ExcelFormatError, match="알 수 없는 부품"):
        parse_upload(content)


def test_parse_upload_rejects_half_filled_side_and_door():
    """방향만 있고 문번호가 없으면(또는 반대) 형식 오류다 — 역 단위 부품인지 문 단위인지 애매해진다."""
    content = _xlsx([
        {"역": "부전", "방향": "부전 방면", "문번호": "", "교체일": "2026-01-15",
         "사유": "고장", "부품": "DCM", "입력자": ""},
    ])
    with pytest.raises(ExcelFormatError):
        parse_upload(content)


def test_parse_upload_rejects_missing_columns():
    import pandas as pd
    from io import BytesIO

    buf = BytesIO()
    pd.DataFrame([{"역": "부전"}]).to_excel(buf, index=False)
    with pytest.raises(ExcelFormatError, match="필요한 열이 없습니다"):
        parse_upload(buf.getvalue())


def test_rows_to_excel_bytes_round_trips_through_parse_upload():
    rows = [{
        "역": "기장", "방향": "부전 방면", "문번호": "2-3", "교체일": "2025-05-01",
        "사유": "함께 교체", "부품": "모터,타이밍벨트", "입력자": "더미",
    }]
    parsed = parse_upload(rows_to_excel_bytes(rows))
    assert parsed[0].station_name == "기장"
    assert parsed[0].reason_code == "with"
    assert parsed[0].part_names == ["모터", "타이밍벨트"]


def test_columns_constant_matches_rows_to_excel_bytes_header():
    import pandas as pd
    from io import BytesIO

    data = rows_to_excel_bytes([{c: "" for c in COLUMNS}])
    df = pd.read_excel(BytesIO(data))
    assert list(df.columns) == COLUMNS
