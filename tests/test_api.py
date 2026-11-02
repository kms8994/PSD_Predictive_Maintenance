"""psd/api.py 검증: 날씨 근거(weather_basis.causes) 표시값.

다른 테스트(test_cycle.py, test_status.py)와 달리 이 파일은 실제 MySQL DB가 필요하다 —
psd/api.py 자체가 DB 없이는 응답을 만들 수 없기 때문이다(psd/db.py가 .env로 바로 연결).
`python scripts/init_db.py`와 `python scripts/compute_cycles.py`를 먼저 실행해 둔 DB가
있어야 통과한다.

코드 리뷰로 찾은 공백 반영: CAUSES_COASTAL/CAUSES_INLAND를 CAUSES 하나로 합쳤을 때
(2026-10-25), weather_basis.causes에 실제로 어떤 값이 나오는지 검증하는 테스트가 하나도
없었다 — CAUSES의 비중 문자열에 오타가 나도 아무 테스트도 못 잡는 상태였다.
"""
from fastapi.testclient import TestClient

from psd.api import CAUSES, app

client = TestClient(app)


def _first_weather_sensitive_part_id() -> int:
    """역 1(부전)의 문 하나에서 DCM 부품 id를 찾는다."""
    station = client.get("/api/stations/1").json()
    door_id = station["sides"]["U"]["doors"][0]["id"]
    door = client.get(f"/api/doors/{door_id}").json()
    dcm = next(u for u in door["units"] if u["part_type_code"] == "DCM")
    return dcm["part_id"]


def test_part_detail_weather_basis_causes_match_causes_dict():
    part_id = _first_weather_sensitive_part_id()
    data = client.get(f"/api/parts/{part_id}").json()
    wb = data["weather_basis"]
    assert wb is not None

    expected = CAUSES["DCM"]
    assert len(wb["causes"]) == len(expected)
    for shown, (name, var, key, weight) in zip(wb["causes"], expected):
        assert shown["name"] == name
        assert shown["variable"] == var
        assert shown["weight_default"] == weight
        assert isinstance(shown["af"], float)


def test_part_detail_observatory_is_a_real_aws_station_name():
    from psd.cycle import AWS_STATION_NAMES

    part_id = _first_weather_sensitive_part_id()
    data = client.get(f"/api/parts/{part_id}").json()
    assert data["weather_basis"]["observatory"] in AWS_STATION_NAMES.values()


# ---------- 2026-10-27: 관리 현황(교체 예정 목록, 최근 교체 기록) ----------


def test_work_items_groups_add_up_and_stay_within_the_period():
    data = client.get("/api/work-items?within_days=730").json()
    assert data["part_count"] == sum(x["count"] for x in data["items"])
    for x in data["items"]:
        # 초록은 기간 안에 든 것만, 그 외(위험·확인 필요·주의)는 기간과 상관없이 들어온다
        assert x["status"] != "초록" or x["days_left"] <= 730
        assert (x["part_id"] is not None) == (x["count"] == 1)


def test_work_items_are_sorted_most_urgent_first():
    from psd.status import RANK

    items = client.get("/api/work-items?within_days=1095").json()["items"]
    keys = [(RANK[x["status"]], x["days_left"]) for x in items]
    assert keys == sorted(keys)


def test_stations_data_quality_counts_parts_without_records():
    data = client.get("/api/stations").json()
    dq = data["data_quality"]
    assert 0 <= dq["no_record"] <= dq["parts"]
    assert dq["parts"] == sum(sum(s["counts"].values()) for s in data["stations"])


# ---------- 2026-11-02: 역 화면의 부품별 상태 표·노선 격자 ----------


def test_station_part_summary_adds_up_and_shows_the_most_urgent_unit():
    from psd.status import RANK

    data = client.get("/api/stations/13").json()
    parts = data["parts"]
    assert sum(p["total"] for p in parts.values()) == sum(data["counts"].values())
    for p in parts.values():
        assert sum(p["counts"].values()) == p["total"]
        # 표시하는 상태는 그 부품 중 가장 급한 것이어야 한다
        assert p["counts"][p["status"]] > 0
        assert all(RANK[s] >= RANK[p["status"]] for s, n in p["counts"].items() if n)
    assert parts["PSC"]["total"] == 1


def test_station_part_summary_lists_every_door_most_urgent_first():
    """2026-11-02: "위험 12개"가 어느 문인지 역 화면에서 바로 보이게 부품 하나하나를 급한 순으로 준다."""
    from psd.status import RANK

    parts = client.get("/api/stations/13").json()["parts"]
    for p in parts.values():
        us = p["units"]
        assert len(us) == p["total"]
        assert us[0]["part_id"] == p["part_id"]  # 맨 앞 = 표에 보이는 가장 급한 부품
        assert [(RANK[u["status"]], u["days_left"]) for u in us] == sorted((RANK[u["status"]], u["days_left"]) for u in us)
    assert all(u["side"] in ("U", "D") and u["no"] for u in parts["IFM"]["units"])
    assert parts["PSC"]["units"][0]["no"] is None
    assert "units" not in client.get("/api/stations").json()["stations"][0]["parts"].get("IFM", {})


def test_station_list_has_part_summary_only_for_stations_in_service():
    for s in client.get("/api/stations").json()["stations"]:
        assert bool(s["parts"]) == s["in_service"]


# ---------- 2026-11-02: 교체 계획(기간별 물량)·기준일·계산 방법 ----------


def test_replacement_plan_3y_matches_the_home_quarter_chart():
    plan = client.get("/api/replacement-plan?span=3y").json()
    vol = client.get("/api/replacement-volume?years_ahead=3").json()
    assert [b["label"] for b in plan["buckets"]] == [q["label"] for q in vol["quarters"]]
    assert [b["total"] for b in plan["buckets"]] == [q["total"] for q in vol["quarters"]]
    assert plan["check"] == vol["check"]


def test_replacement_plan_station_counts_add_up_per_period():
    for span, n in [("12m", 13), ("3y", 13), ("5y", 6)]:
        plan = client.get(f"/api/replacement-plan?span={span}").json()
        assert len(plan["buckets"]) == n
        for b in plan["buckets"]:
            assert b["total"] == sum(b["counts"].values()) == sum(s["count"] for s in b["stations"])
            counts = [s["count"] for s in b["stations"]]
            assert counts == sorted(counts, reverse=True)


def test_replacement_plan_station_rows_break_down_by_part():
    plan = client.get("/api/replacement-plan?span=3y").json()
    for b in plan["buckets"]:
        for s in b["stations"]:
            assert sum(s["parts"].values()) == s["count"]
        for code, n in b["counts"].items():
            assert sum(s["parts"].get(code, 0) for s in b["stations"]) == n


def test_replacement_plan_export_matches_the_screen():
    import io

    import openpyxl

    r = client.get("/api/replacement-plan/export?span=3y")
    assert r.status_code == 200
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames[:3] == ["부품별", "역별", "역x부품"]
    plan = client.get("/api/replacement-plan?span=3y").json()
    total = sum(b["total"] for b in plan["buckets"])
    last = [c.value for c in wb["부품별"][wb["부품별"].max_row]]
    assert last[0] == "합계" and last[-2] == total
    assert last[-1] == sum(plan["recent_failures"].values())  # 마지막 열 = 예비품 참고(지난 1년 고장 교체)
    assert sum(row[3] for row in wb["역x부품"].iter_rows(min_row=2, values_only=True)) == total
    assert client.get("/api/replacement-plan/export?span=7y").status_code == 400


def test_replacement_plan_rejects_unknown_span():
    assert client.get("/api/replacement-plan?span=7y").status_code == 400


def test_today_is_the_date_statuses_are_computed_for():
    assert client.get("/api/today").json()["today"] == client.get("/api/work-items").json()["today"]


def test_work_items_rejects_negative_period():
    assert client.get("/api/work-items?within_days=-1").status_code == 400


def test_recent_replacement_records_are_newest_first_and_capped():
    data = client.get("/api/replacement-records?limit=5").json()
    assert len(data["records"]) <= 5
    assert len(data["records"]) <= data["total"]
    assert all(r["parts"] for r in data["records"])
    order = [(r["created_at"], r["replaced_on"], r["id"]) for r in data["records"]]
    assert order == sorted(order, reverse=True)


def test_recent_replacement_records_by_replaced_date():
    data = client.get("/api/replacement-records?limit=10&order=replaced").json()
    order = [(r["replaced_on"], r["id"]) for r in data["records"]]
    assert order == sorted(order, reverse=True)
    assert client.get("/api/replacement-records?order=wrong").status_code == 400


# ---------- 2026-10-27: 역별 PDF 보고서 ----------


def test_station_report_pdf_is_a_readable_two_page_pdf():
    import io

    from pypdf import PdfReader

    r = client.get("/api/stations/1/report.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert "filename*=UTF-8''" in r.headers["content-disposition"]
    reader = PdfReader(io.BytesIO(r.content))
    assert len(reader.pages) == 2
    text = reader.pages[0].extract_text()
    assert "부전역 PSD 부품 교체 현황 보고서" in text


def test_all_stations_report_pdf_has_every_operating_station():
    import io

    from pypdf import PdfReader

    from psd.cycle import STATIONS

    r = client.get("/api/report.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    text = "".join(page.extract_text() for page in PdfReader(io.BytesIO(r.content)).pages)
    for s in STATIONS:
        if s.in_service:
            assert f"{s.name}역 PSD 부품 교체 현황 보고서" in text


def test_station_report_pdf_not_in_service_or_unknown_station():
    assert client.get("/api/stations/24/report.pdf").status_code == 409  # 북울산(개통 전)
    assert client.get("/api/stations/999999/report.pdf").status_code == 404


# ---------- 2026-10-25: 엑셀 업로드 + 보고서 출력 ----------


def test_report_download_for_all_stations_is_a_readable_excel():
    """2026-11-02 다시 짠 보고서: 요약(주문 수량) · 교체 작업(문 묶음) · 역별 주문 · 전체 부품."""
    import io

    import openpyxl
    import pandas as pd

    from psd.cycle import STATIONS

    r = client.get("/api/report")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats")
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["요약", "교체 작업", "역별 주문", "전체 부품"]

    parts = pd.read_excel(io.BytesIO(r.content), sheet_name="전체 부품")
    assert list(parts.columns) == ["역", "방향", "문", "부품", "상태", "다음 교체일", "남은 일수", "남은 기간", "교체주기(년)", "마지막 교체일", "기준"]
    assert parts["역"].nunique() == len([s for s in STATIONS if s.in_service])  # 개통 전 역(북울산)은 부품이 없다
    assert set(parts["상태"]) <= {"교체 임박", "주의", "안전", "확인 필요"}  # 코드값이 아니라 화면과 같은 이름
    assert pd.api.types.is_datetime64_any_dtype(parts["다음 교체일"])  # 문자열이 아니라 엑셀 날짜형

    work = pd.read_excel(io.BytesIO(r.content), sheet_name="교체 작업")
    soon = parts[parts["상태"] != "안전"]
    assert work["수량"].sum() == len(soon) + ((parts["상태"] == "안전") & (parts["남은 일수"] <= 365)).sum()

    # 요약의 주문 수량 합계 = 현황 화면 교체 예정(1년 안) 부품 수
    summary = wb["요약"]
    total_row = next(row for row in summary.iter_rows(values_only=True) if row[0] == "합계")
    home = client.get("/api/work-items?within_days=365").json()
    assert total_row[3] == sum(x["count"] for x in home["items"] if x["days_left"] <= 365 and x["status"] != "확인_필요")


def test_report_download_for_one_station_is_scoped_to_that_station():
    import io

    import openpyxl
    import pandas as pd

    r = client.get("/api/report?station_id=13")
    assert r.status_code == 200
    assert openpyxl.load_workbook(io.BytesIO(r.content)).sheetnames == ["요약", "교체 작업", "전체 부품"]  # 역 하나면 역별 주문 없음
    assert set(pd.read_excel(io.BytesIO(r.content), sheet_name="전체 부품")["역"]) == {"오시리아"}
    work = pd.read_excel(io.BytesIO(r.content), sheet_name="교체 작업")
    detail = client.get("/api/stations/13").json()["parts"]
    urgent = [u for p in detail.values() for u in p["units"] if u["status"] != "초록" or u["days_left"] <= 365]
    assert work["수량"].sum() == len(urgent)


def test_report_download_for_unknown_station_is_404():
    r = client.get("/api/report?station_id=999999")
    assert r.status_code == 404


def test_upload_unknown_station_name_is_rejected_without_saving_anything():
    from psd.excel_io import rows_to_excel_bytes

    content = rows_to_excel_bytes([{
        "역": "존재하지않는역", "방향": "", "문번호": "", "교체일": "2026-01-01",
        "사유": "고장", "부품": "종합제어반 릴레이", "입력자": "",
    }])
    r = client.post(
        "/api/replacement-records/sync",
        files={"file": ("t.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert r.status_code == 400
    body = r.json()["detail"]
    assert "존재하지않는역" in str(body)


def test_upload_rejects_excel_missing_required_columns():
    import pandas as pd
    from io import BytesIO

    buf = BytesIO()
    pd.DataFrame([{"역": "부전"}]).to_excel(buf, index=False)
    r = client.post(
        "/api/replacement-records/sync",
        files={"file": ("t.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert r.status_code == 400
    assert "필요한 열" in r.json()["detail"]


# ---------- 2026-11-01: 엑셀이 원본(올리면 DB를 엑셀에 맞춘다) ----------

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_exported_records_compare_as_unchanged():
    """전체 기록을 내려받아 그대로 다시 올리면 바뀌는 게 없어야 한다(내려받기 ↔ 올리기 형식이 같다)."""
    exported = client.get("/api/replacement-records/export")
    assert exported.status_code == 200
    r = client.post("/api/replacement-records/compare", files={"file": ("all.xlsx", exported.content, XLSX)}).json()
    total = client.get("/api/replacement-records?limit=1").json()["total"]
    assert r["counts"] == {"new": 0, "changed": 0, "missing": 0, "db_duplicates": 0, "same": total, "file_duplicates": 0}
    assert r["changes"] == []


def test_compare_does_not_save_anything():
    from psd.excel_io import rows_to_excel_bytes

    before = client.get("/api/replacement-records?limit=1").json()["total"]
    content = rows_to_excel_bytes([{
        "역": "부전", "방향": "", "문번호": "", "교체일": "2026-01-01",
        "사유": "고장", "부품": "종합제어반 릴레이", "입력자": "",
    }])
    r = client.post("/api/replacement-records/compare", files={"file": ("t.xlsx", content, XLSX)}).json()
    assert r["counts"]["new"] + r["counts"]["same"] == 1
    assert "batch_id" not in r
    assert client.get("/api/replacement-records?limit=1").json()["total"] == before


def test_compare_rejects_date_before_psd_start():
    """2026-11-02 QA: 사용개시 전 날짜(연도 오타)가 들어가면 설치일이 그날로 바뀌어 부품이 수천 일 지난 위험이 됐다."""
    from psd.excel_io import rows_to_excel_bytes

    content = rows_to_excel_bytes([{
        "역": "부전", "방향": "", "문번호": "", "교체일": "1990-01-01",
        "사유": "고장", "부품": "종합제어반 릴레이", "입력자": "",
    }])
    r = client.post("/api/replacement-records/compare", files={"file": ("t.xlsx", content, XLSX)})
    assert r.status_code == 400
    assert "PSD 사용개시일" in str(r.json()["detail"])


def test_simulate_rejects_non_finite_change():
    assert client.get("/api/weather-impact/simulate?station_id=1&d_temp=nan").status_code == 400


def test_sample_preview_has_no_records_missing_from_the_excel():
    """정비기록 시연 샘플엔 그 역·기간의 기존 기록도 들어 있어, 올려도 "엑셀에 없는 기록"이 없어야 한다
    (빠져 있으면 시연 때 기존 기록 수백 건이 지울 후보로 뜬다 — 2026-11-02)."""
    r = client.post("/api/sample-records/preview").json()
    assert r["counts"]["missing"] == 0 and not r["missing_warn"]
    assert r["counts"]["new"] + r["counts"]["same"] == len(client.get("/api/sample-records").json()["rows"])


def test_template_has_empty_record_sheet_and_separate_examples():
    import io

    import pandas as pd

    from psd.excel_io import COLUMNS

    xl = pd.ExcelFile(io.BytesIO(client.get("/api/replacement-records/template").content))
    assert xl.sheet_names == ["기록", "작성 예시"]
    records, examples = (pd.read_excel(xl, s) for s in xl.sheet_names)
    assert list(records.columns) == COLUMNS and records.empty  # 업로드는 첫 시트만 읽는다
    assert len(examples) == 3


def test_record_list_filters_by_station_and_part_and_pages():
    first = client.get("/api/replacement-records?limit=3&order=replaced&station_id=3&part=IFM").json()
    second = client.get("/api/replacement-records?limit=3&offset=3&order=replaced&station_id=3&part=IFM").json()
    assert first["total"] == second["total"]
    for r in first["records"] + second["records"]:
        assert r["station_id"] == 3 and "IFM 릴레이" in r["parts"]
    assert not {r["id"] for r in first["records"]} & {r["id"] for r in second["records"]}


def test_record_list_filters_by_date_range_and_reason():
    r = client.get("/api/replacement-records?limit=200&order=replaced&date_from=2026-01-01&date_to=2026-06-30&reason=fail").json()
    assert r["total"] > 0
    assert all("2026-01-01" <= x["replaced_on"] <= "2026-06-30" and x["reason"] == "fail" for x in r["records"])
    assert client.get("/api/replacement-records?reason=broken").status_code == 400


def test_compare_scope_names_the_excel_rows_at_both_ends():
    """2026-11-02: 범위가 왜 넓어졌는지 찾을 수 있게 가장 이른·늦은 날짜가 엑셀 몇 행인지 준다."""
    from psd.excel_io import rows_to_excel_bytes

    row = {"역": "부전", "방향": "", "문번호": "", "사유": "점검(이상 없음)", "부품": "전체", "입력자": ""}
    content = rows_to_excel_bytes([{**row, "교체일": "2026-10-01"}, {**row, "교체일": "2024-03-05"}, {**row, "교체일": "2026-10-20"}])
    scope = client.post("/api/replacement-records/compare", files={"file": ("t.xlsx", content, XLSX)}).json()["scope"]
    assert (scope["from"], scope["from_row"], scope["to"], scope["to_row"]) == ("2024-03-05", 3, "2026-10-20", 4)


def test_sync_records_who_uploaded():
    """2026-11-02: 업로드 이력에 올린 사람을 남긴다. 저장하지 않게 세션을 직접 열고 롤백한다."""
    from psd.api import _sync
    from psd.db import get_session
    from psd.excel_io import rows_to_excel_bytes
    from psd.models import UploadBatch

    content = rows_to_excel_bytes([{"역": "부전", "방향": "", "문번호": "", "교체일": "2026-10-02",
                                    "사유": "점검(이상 없음)", "부품": "전체", "입력자": ""}])
    s = get_session()
    try:
        r = _sync(s, content, "t.xlsx", False, uploaded_by="  홍길동 ")
        if r["batch_id"]:  # 이미 같은 기록이 있으면 이력이 안 생긴다
            assert s.get(UploadBatch, r["batch_id"]).uploaded_by == "홍길동"
    finally:
        s.rollback()
        s.close()


def test_replacement_plan_reports_last_year_failures_for_spare_parts():
    plan = client.get("/api/replacement-plan?span=12m").json()
    from psd.catalog import PART_TYPES

    assert set(plan["recent_failures"]) == {p["code"] for p in PART_TYPES}
    assert all(n >= 0 for n in plan["recent_failures"].values())


def test_station_detail_shows_psd_install_date_separately():
    from psd.calibrate import PSD_START

    st = client.get("/api/stations/13").json()["station"]
    assert st["psd_install_date"] == PSD_START[13].isoformat()


def test_undo_unknown_upload_is_404():
    assert client.post("/api/upload-batches/999999/undo").status_code == 404
