"""psd/excel_io.py 검증: 코레일 장애 이력 양식 읽기·쓰기 (2026-11-24 v3, 데이터셋_v3_설계.md 1·5장).

소분류는 예시 분류표와 실제 분류표에 함께 있는 것(모터&엔코더, 밸트·스크류, DCU(MCU), 종합제어반)만 쓴다.
"""
from datetime import date, timedelta
from io import BytesIO

import pytest

from psd.excel_io import COLUMNS, ExcelFormatError, parse_track, parse_upload, record_row, rows_to_excel_bytes


def _row(**kw) -> dict:
    base = {"년도": 2026, "월": 1, "일": 15, "노선": "동해선", "역": "부전", "장애인지": "사후조치", "승강장 선로": "상본선(#3)",
            "출입문": "1-1", "장애부위②(소분류)": "모터&엔코더", "장애내용①": "고장", "조치사항": "교체", "조치자": "정비1팀"}
    return {**base, **kw}


def _xlsx(rows: list[dict]) -> bytes:
    return rows_to_excel_bytes(rows)


def _one(**kw):
    return parse_upload(_xlsx([_row(**kw)])).rows[0]


def test_reads_door_level_row():
    r = _one(본부="부산경남", PSD설치="지상", 설치년도=2022, 고장원인="마모", **{"조치사항 상세": "모터 교체"})
    assert (r.station_name, r.replaced_on, r.reason_code, r.unit_code, r.action_code) == ("부전", date(2026, 1, 15), "fail", "MOTOR", "full")
    assert (r.side, r.door_no, r.track_raw) == ("U", "1-1", "상본선(#3)")
    assert (r.symptom, r.cause_text, r.action_detail, r.created_by) == ("고장", "마모", "모터 교체", "정비1팀")
    assert r.station_info == {"hq": "부산경남", "line_name": "동해선", "structure": "지상", "install_year": 2022}
    assert r.warnings == []


def test_station_level_unit_ignores_door():
    r = _one(**{"장애부위②(소분류)": "종합제어반", "출입문": "", "승강장 선로": "", "장애인지": "사전조치", "조치사항": "단순교체",
                "장애내용①": ""})
    assert (r.unit_code, r.side, r.door_no, r.reason_code, r.action_code) == ("PSC", None, None, "prev", "partial")


@pytest.mark.parametrize("text, side", [("상본선(#3)", "U"), ("하선(#1)", "D"), ("상부본선(#4)", "UB"), ("하부본선(#1)", "DB"),
                                        ("상", "U"), ("하행", "D"), ("상 본선", "U"), ("1번선", None), ("", None)])
def test_parse_track(text, side):
    assert parse_track(text) == side


def test_unit_name_variants_resolve():
    """띄어쓰기·쉼표·'벨트/밸트' 차이, 괄호 뺀 이름도 같은 소분류로 읽는다."""
    assert _one(**{"장애부위②(소분류)": "벨트,스크류"}).unit_code == "BELT"
    assert _one(**{"장애부위②(소분류)": "DCU"}).unit_code == "DCU"


@pytest.mark.parametrize("kw, msg", [
    ({"장애부위②(소분류)": "없는부위"}, "분류표에 없는"),
    ({"장애인지": "고장"}, "장애인지는"),
    ({"조치사항": "갈아끼움"}, "조치사항은"),
    ({"승강장 선로": ""}, "승강장 선로가 필요"),
    ({"출입문": "가-1"}, "차량-문"),
    ({"월": 13}, "날짜로 읽을 수 없습니다"),
    ({"역": ""}, "역 이름이 비어"),
])
def test_rejects_bad_rows(kw, msg):
    with pytest.raises(ExcelFormatError, match=msg):
        parse_upload(_xlsx([_row(**kw)]))


def test_rejects_future_date():
    d = date.today() + timedelta(days=30)
    with pytest.raises(ExcelFormatError, match="미래일 수 없습니다"):
        parse_upload(_xlsx([_row(년도=d.year, 월=d.month, 일=d.day)]))


def test_collects_all_bad_rows_in_one_error():
    with pytest.raises(ExcelFormatError, match="2개 줄이 틀렸습니다"):
        parse_upload(_xlsx([_row(조치사항="x"), _row(), _row(장애인지="x")]))


def test_warnings_do_not_block():
    """장애형태가 조치 규칙과 다름·대분류 불일치·목록에 없는 장애내용은 경고만 하고 받는다."""
    r = _one(장애형태="예방보수", **{"장애부위①(대분류)": "전원 장애", "장애내용①": "이상한 소리"})
    assert len(r.warnings) == 3
    assert r.action_code == "full" and r.symptom == "이상한 소리"


def test_door_number_turned_into_date_by_excel_is_recovered():
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "고장현황 전체"
    ws.append(COLUMNS)
    row = record_row(day=date(2026, 1, 15), station="부전", reason="fail", unit_code="MOTOR", action="full", side="U", door_no="2-1")
    values = [row[c] for c in COLUMNS]
    values[COLUMNS.index("출입문")] = date(2026, 2, 1)  # 엑셀이 "2-1"을 2월 1일로 바꿔 버린 경우
    ws.append(values)
    buf = BytesIO()
    wb.save(buf)
    r = parse_upload(buf.getvalue()).rows[0]
    assert r.door_no == "2-1" and r.warnings


def test_skips_blank_formula_rows_and_other_lines_and_finds_header_below_title():
    """양식처럼 위에 제목 줄이 있고 머리글이 아래, 빈 줄(수식만)이 섞이고 다른 노선 줄이 있어도 읽는다."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "고장현황 전체"
    ws.append(["승강장안전문 장애발생 및 보수이력 현황"])
    for _ in range(3):
        ws.append([])
    ws.append([c.replace("장애부위②(소분류)", "장애부위②\n(소분류)") for c in COLUMNS])
    for r in (_row(), {}, _row(노선="경부선", 역="서울"), _row(일=16)):
        ws.append([r.get(c, "") for c in COLUMNS])
    buf = BytesIO()
    wb.save(buf)
    res = parse_upload(buf.getvalue())
    assert res.header_row == 5 and res.skipped_other_line == 1
    assert [x.row_no for x in res.rows] == [6, 9]


def test_rejects_non_excel_and_missing_columns():
    with pytest.raises(ExcelFormatError, match="엑셀 파일을 읽을 수 없습니다"):
        parse_upload(b"not an excel file")
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["역", "장애부위②(소분류)"])
    buf = BytesIO()
    wb.save(buf)
    with pytest.raises(ExcelFormatError, match="필요한 칸이 없습니다"):
        parse_upload(buf.getvalue())


def test_record_row_round_trips_through_parse_upload():
    row = record_row(day=date(2025, 5, 1), station="기장", reason="prev", unit_code="BELT", action="adjust", side="DB", door_no="2-3",
                     track_raw="하부본선(#4)", symptom="늘어짐", cause_text="마모", action_detail="장력 조정", receiver="홍길동",
                     received="5월 1일 08:00", completed="5월 1일 09:00", worker="정비2팀", station_info={"structure": "지상"})
    r = parse_upload(rows_to_excel_bytes([row])).rows[0]
    assert (r.station_name, r.reason_code, r.unit_code, r.action_code, r.side, r.door_no) == ("기장", "prev", "BELT", "adjust", "DB", "2-3")
    assert (r.receiver, r.received, r.completed, r.created_by) == ("홍길동", "5월 1일 08:00", "5월 1일 09:00", "정비2팀")
    assert row["장애형태"] == "보수" and r.warnings == []


def test_header_matches_columns():
    from openpyxl import load_workbook

    ws = load_workbook(BytesIO(rows_to_excel_bytes([]))).active
    assert ws.title == "고장현황 전체"
    assert [c.value for c in ws[1]] == COLUMNS
