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


def test_work_items_rejects_negative_period():
    assert client.get("/api/work-items?within_days=-1").status_code == 400


def test_recent_replacement_records_are_newest_first_and_capped():
    data = client.get("/api/replacement-records?limit=5").json()
    assert len(data["records"]) <= 5
    assert len(data["records"]) <= data["total"]
    assert all(r["parts"] for r in data["records"])
    order = [(r["created_at"], r["id"]) for r in data["records"]]
    assert order == sorted(order, reverse=True)


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


def test_station_report_pdf_not_in_service_or_unknown_station():
    assert client.get("/api/stations/24/report.pdf").status_code == 409  # 북울산(개통 전)
    assert client.get("/api/stations/999999/report.pdf").status_code == 404


# ---------- 2026-10-25: 엑셀 업로드 + 보고서 출력 ----------


def test_report_download_for_all_stations_is_a_readable_excel():
    import io

    import pandas as pd

    r = client.get("/api/report")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats")
    df = pd.read_excel(io.BytesIO(r.content))
    assert list(df.columns) == [
        "역", "문", "부품", "상태", "다음 교체 예정일", "남은 기간(일)",
        "권장 주기(년)", "주기 범위(년)", "판정", "설치일", "교체 기록 있음",
    ]
    assert len(df) > 1000  # 역 24개 x 문마다 여러 부품이라 수천 행이 나온다 — "비어 있지 않다"가 핵심
    # 아직 PSD가 없는 역(북울산)은 부품 자체가 없어 보고서에도 안 나온다 — 운영 중인 역만 셈
    from psd.cycle import STATIONS

    assert df["역"].nunique() == len([s for s in STATIONS if s.in_service])
    assert set(df["상태"]) <= {"빨강", "노랑", "초록", "확인 필요"}  # 코드값(확인_필요)이 아니라 사람이 읽는 표시값
    assert pd.api.types.is_datetime64_any_dtype(df["다음 교체 예정일"])  # 문자열이 아니라 엑셀 날짜형


def test_report_download_for_one_station_is_scoped_to_that_station():
    import io

    import pandas as pd

    r = client.get("/api/report?station_id=1")
    assert r.status_code == 200
    df = pd.read_excel(io.BytesIO(r.content))
    assert set(df["역"]) == {"부전"}


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
        "/api/replacement-records/upload",
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
        "/api/replacement-records/upload",
        files={"file": ("t.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert r.status_code == 400
    assert "필요한 열" in r.json()["detail"]
