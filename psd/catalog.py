"""DB 초기 데이터로 쓰는 고정 목록 (관측소, 부품 종류, 문 배치).

역 목록(STATIONS)은 `psd/cycle.py`에 있다. 관측소 코드(OBS_CODE)도 같은 곳에 있지만
2026-10-25부터 날씨 계산에는 안 쓰고 DB 적재(scripts/init_db.py)용으로만 쓴다 — 실제
날씨 계산은 STATIONS의 aws_stn·AWS_STATION_NAMES(둘 다 psd/cycle.py)를 기준으로 한다.
"""

OBSERVATORIES = [
    {"id": "159", "kind": "ASOS", "name": "부산(추정)"},
    {"id": "152", "kind": "ASOS", "name": "울산(추정)"},
]

# 코드, 이름, 설치 위치(문/역), 날씨 계산 여부 (설계_v2.md 4-2, 4단계_계산공식_근거조사.md 1장)
PART_TYPES = [
    {"code": "MOTOR", "name": "모터", "unit": "door", "weather_sensitive": True},
    {"code": "BELT", "name": "타이밍벨트", "unit": "door", "weather_sensitive": False},
    {"code": "PULLEY", "name": "풀리", "unit": "door", "weather_sensitive": False},
    {"code": "HANGER", "name": "도어 행거", "unit": "door", "weather_sensitive": False},
    {"code": "DCM", "name": "DCM", "unit": "door", "weather_sensitive": True},
    {"code": "IFM", "name": "IFM 릴레이", "unit": "door", "weather_sensitive": True},
    {"code": "PSC", "name": "종합제어반 릴레이", "unit": "station", "weather_sensitive": False},
]

NAME_TO_CODE = {p["name"]: p["code"] for p in PART_TYPES}
DOOR_PART_CODES = [p["code"] for p in PART_TYPES if p["unit"] == "door"]
STATION_PART_CODES = [p["code"] for p in PART_TYPES if p["unit"] == "station"]

# 방향별 4량 × 4문 = 16개(임시값, 설계_v2.md 5단계: 역별 실제 문 수는 멘토 확인 대기)
SIDES = {"U": "부전 방면", "D": "북울산 방면"}
DOORS_PER_SIDE = 16
CARS_PER_SIDE = 4
