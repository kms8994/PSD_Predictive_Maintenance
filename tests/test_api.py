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
