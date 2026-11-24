"""psd/stations.py 검증: 역 목록 고정 파일과 역 환경 파일 읽기 (2026-11-24). DB 없이 돈다."""
from datetime import date

import pytest
from openpyxl import load_workbook

from psd import stations as st


def _copy(tmp_path, src, edit) -> object:
    wb = load_workbook(src)
    edit(wb)
    out = tmp_path / src.name
    wb.save(out)
    return out


def _station_rows(wb):
    return {r[2].value: r for r in wb["역 목록"].iter_rows(min_row=2)}


def test_station_file_loads_in_line_order_with_permanent_ids():
    assert [s.seq for s in st.STATIONS] == sorted(s.seq for s in st.STATIONS)
    assert len({s.id for s in st.STATIONS}) == len(st.STATIONS)
    assert st.STATIONS[0].name == "부전" and st.LINE_NAME == "동해선"
    assert all(s.aws_stn in st.AWS_STATION_NAMES for s in st.STATIONS)
    assert all(s.psd_start >= s.open_date for s in st.STATIONS)


def test_old_names_resolve_to_the_same_station():
    gaeunpo = next(s for s in st.STATIONS if s.name == "개운포")
    assert st.STATION_ID_BY_NAME["선암"] == gaeunpo.id


def test_new_station_in_the_middle_only_shifts_order(tmp_path):
    """역이 중간에 생기면 순서만 다시 매기고 기존 ID는 그대로 둔다."""
    def edit(wb):
        rows = _station_rows(wb)
        rows["북울산"][1].value = 25
        wb["역 목록"].append([25, 24, "새역", "", date(2027, 1, 1), None, "943", "개통 전", "아니오", ""])
    stations = st.load_station_file(_copy(tmp_path, st.STATION_FILE, edit))
    by_name = {s.name: s for s in stations}
    assert (by_name["새역"].id, by_name["새역"].seq, by_name["북울산"].seq) == (25, 24, 25)
    assert [s.name for s in stations][-2:] == ["새역", "북울산"]


@pytest.mark.parametrize("edit, msg", [
    (lambda wb: setattr(_station_rows(wb)["거제"][0], "value", 1), "ID 1가"),
    (lambda wb: setattr(_station_rows(wb)["거제"][1], "value", 1), "순서 1가"),
    (lambda wb: setattr(_station_rows(wb)["거제"][6], "value", "999"), "AWS 지점"),
    (lambda wb: setattr(_station_rows(wb)["거제"][7], "value", "폐역"), "운영은"),
    (lambda wb: setattr(_station_rows(wb)["거제"][3], "value", "부전"), "이름 '부전'"),
    (lambda wb: setattr(_station_rows(wb)["거제"][5], "value", date(2010, 1, 1)), "개통일"),
])
def test_bad_station_file_is_rejected(tmp_path, edit, msg):
    with pytest.raises(st.StationFileError, match=msg):
        st.load_station_file(_copy(tmp_path, st.STATION_FILE, edit))


def test_sample_environment_has_categories_but_no_values():
    env = st.read_environment(st.ENV_SAMPLE, "sample")
    assert "구조" in env.categories and not env.warnings
    assert all(not v for v in env.values.values())
    block = env.for_station(st.STATIONS[0].id)
    assert block[0]["category"] == "구조" and all(i["value"] is None for i in block[0]["items"])


def test_environment_values_are_checked_and_bad_cells_become_warnings(tmp_path):
    """선택지에 없는 값·숫자가 아닌 값은 그 칸만 비우고 경고로 남긴다. 항목을 더하면 새 카테고리가 생긴다."""
    def edit(wb):
        wb["항목"].append(["이용", "하루 이용객", "숫자", "", "명", ""])
        v = wb["역 환경"]
        head = [c.value for c in v[1]]
        v.cell(1, len(head) + 1, "하루 이용객")
        rows = {r[0].value: r for r in v.iter_rows(min_row=2)}
        rows[1][head.index("역 구조")].value = "지상"
        rows[2][head.index("역 구조")].value = "공중"  # 선택지에 없음
        v.cell(rows[1][0].row, len(head) + 1, 12000)
        v.cell(rows[2][0].row, len(head) + 1, "많음")  # 숫자가 아님
    env = st.read_environment(_copy(tmp_path, st.ENV_SAMPLE, edit), "test")
    assert env.categories == ["구조", "이용"]
    assert env.values[1] == {"역 구조": "지상", "하루 이용객": 12000}
    assert env.values[2] == {}
    assert len(env.warnings) == 2
