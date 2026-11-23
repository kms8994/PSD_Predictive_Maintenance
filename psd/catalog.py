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

# ---------- 정비기록 세부 항목 (2026-11-16, 데이터셋_v2_설계.md 1장) ----------
# 사유(왜 했나)와 조치(무엇을 했나)를 나눈다. 조치에 따라 부품이 "새것"으로 돌아가는 정도가 달라
# 수명 데이터의 뜻이 바뀐다(향후_방향_MVP이후.md 5절). 설치일·주기 보정은 전체 교체만 새 수명 시작으로 본다.
ACTIONS = {
    "full": "전체 교체",
    "partial": "일부 교체",
    "overhaul": "전체 수리",
    "repair": "일부 수리",
    "none": "없음",  # 점검(이상 없음)만
}
ACTION_ALIASES = {"조정": "repair", "청소": "repair", "재체결": "repair", "윤활": "repair", "오버홀": "overhaul",
                  "분해 정비": "overhaul", "교체": "full"}
SUB_PART_ACTIONS = {"partial", "repair"}  # 세부 부품을 적는 조치. 일부 교체는 꼭 적어야 한다

# 세부 부품(가정 — 일반적인 PSD 구동부 구성으로 정한 임시 목록, 멘토 확인 필요. 코레일 부품 리스트를 옮긴 것이 아님).
# 이름이 부품 사이에 겹치지 않아 이름만으로 어느 부품의 것인지 정해진다. 타이밍벨트는 세부 부품이 없다(일부 수리 = 장력 조정)
SUB_PARTS = [
    {"code": "MOT_BRG", "name": "모터 베어링", "part": "MOTOR"},
    {"code": "MOT_ENC", "name": "엔코더", "part": "MOTOR"},
    {"code": "MOT_WIRE", "name": "모터 배선·커넥터", "part": "MOTOR"},
    {"code": "PUL_BRG", "name": "풀리 베어링", "part": "PULLEY"},
    {"code": "HNG_ROLL", "name": "행거 롤러", "part": "HANGER"},
    {"code": "HNG_SHOE", "name": "가이드 슈", "part": "HANGER"},
    {"code": "DCM_CAP", "name": "전원부 콘덴서", "part": "DCM"},
    {"code": "DCM_BRD", "name": "제어 보드", "part": "DCM"},
    {"code": "DCM_CONN", "name": "DCM 커넥터·퓨즈", "part": "DCM"},
    {"code": "IFM_CONT", "name": "IFM 릴레이 접점", "part": "IFM"},
    {"code": "PSC_MOD", "name": "릴레이 모듈", "part": "PSC"},
    {"code": "PSC_TERM", "name": "단자대", "part": "PSC"},
]
SUB_PART_NAME_TO_CODE = {s["name"]: s["code"] for s in SUB_PARTS}
SUB_PART_CODE_TO_NAME = {s["code"]: s["name"] for s in SUB_PARTS}
SUB_PART_OF = {s["code"]: s["part"] for s in SUB_PARTS}  # 세부 부품 → 부품 종류 코드

# 증상(고장일 때). parts는 주로 나오는 부품 — 더미 생성에 쓰고, 입력 검사는 하지 않는다(현장이 다르게 볼 수 있음)
SYMPTOMS = {
    "OPEN": {"name": "열림·닫힘 불량", "parts": ["MOTOR", "BELT", "PULLEY", "HANGER", "DCM"]},
    "NOISE": {"name": "소음·진동", "parts": ["MOTOR", "PULLEY", "HANGER", "BELT"]},
    "SAG": {"name": "문 처짐·걸림", "parts": ["HANGER", "PULLEY"]},
    "SENSE": {"name": "장애물 감지 오작동", "parts": ["DCM", "IFM"]},
    "COMM": {"name": "제어·통신 이상", "parts": ["DCM", "IFM", "PSC"]},
    "LAMP": {"name": "표시등·경보 이상", "parts": ["IFM", "PSC"]},
}
SYMPTOM_NAME_TO_CODE = {v["name"]: k for k, v in SYMPTOMS.items()}

# 원인(현장 추정, 고장일 때). 날씨 공식의 원인과 맞춰 볼 수 있게 나눴다: 콘덴서·권선·납땜 = 열, Peck = 습기·부식
CAUSES = {"WEAR": "마모", "HEAT": "열", "HUMID": "습기·부식", "DUST": "이물·먼지", "SHOCK": "외부 충격", "UNKNOWN": "원인 미상"}
CAUSE_NAME_TO_CODE = {v: k for k, v in CAUSES.items()}
CAUSE_ALIASES = {"미상": "UNKNOWN", "모름": "UNKNOWN", "부식": "HUMID", "습기": "HUMID", "이물": "DUST", "먼지": "DUST", "충격": "SHOCK"}
