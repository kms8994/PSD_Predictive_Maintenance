"""DB 초기 데이터로 쓰는 고정 목록: 예측 단위(장애부위 소분류), 문 배치, 양식의 조치·장애인지 규칙.

2026-11-24 v3(데이터셋_v3_설계.md): 예측 단위를 우리 7종 부품에서 코레일 장애 이력 양식의 **장애부위 소분류**로 바꿨다.
소분류 목록 자체는 분류표 파일(psd/taxonomy.py)에서 읽는다 — 코레일 자료라 코드에 적지 않는다.
역 목록(STATIONS)과 날씨 지점(aws_stn·AWS_STATION_NAMES)은 `psd/cycle.py`에 있다.
"""
from psd.taxonomy import load as _load_taxonomy

TAXONOMY = _load_taxonomy()

# ---------- 예측 대상 (2026-11-28, 미팅/코레일_피드백_정리_1127.md 2장) ----------
# 코레일 담당자: 구동부·센서부를 중점으로, 통신(RF장치·TIP)과 제어기 DCU, 가능하면 가이드레일.
# 도어는 눈에 잘 보여 필요 없다(가동문 중 가이드레일 장애내용만 따로 — 분류표 splits).
# 대상이 아닌 소분류의 엑셀 줄은 읽을 때 건너뛰고 몇 줄인지 알려 준다(psd/excel_io.py). 양식 자체는 그대로 받는다.
TARGET_GROUPS = ("구동부 장애", "센서 장애", "통신 장애")  # 대분류 전체가 대상
TARGET_CODES = ("DCU", "GUIDE")  # 대분류는 대상이 아니지만 이 소분류만 대상


def is_target(u) -> bool:
    return u.group in TARGET_GROUPS or u.code in TARGET_CODES


# 소분류 = 예측 단위. 이름을 바꾸지 않고 "PART_TYPES"로 둔다(DB 표 part_types, 화면의 "부품" 열과 같은 자리).
# 예측 대상만 담는다. 순서는 분류표 순서(갈라 낸 가이드레일은 원래 소분류 가동문 자리).
# weather_sensitive: 날씨 공식으로 주기가 역마다 달라지는지(motor·dcm). fixed(10년)와 공식 없음(null)은 아니다
# form_name: 엑셀 '장애부위②(소분류)'에 쓰는 이름(가이드레일 → 가동문)
_ORDER = {u.code: i for i, u in enumerate(TAXONOMY.form_units)}


def display_name(u) -> str:
    """화면 이름. 양식의 '기타'처럼 대분류 없이는 무엇인지 모르는 이름은 대분류를 붙인다(구동부 장애 › 기타 → "구동부 기타")."""
    return f"{u.group.removesuffix(' 장애')} {u.name}" if u.name == "기타" else u.name


PART_TYPES = [
    {"code": u.code, "name": display_name(u), "group": u.group, "unit": u.unit, "formula": u.formula,
     "weather_sensitive": u.formula in ("motor", "dcm"), "symptoms": list(u.symptoms), "form_name": TAXONOMY.form_name(u.code)}
    for u in sorted((u for u in TAXONOMY.units if is_target(u)), key=lambda u: _ORDER[u.split_from or u.code])
]
TARGET_SET = {p["code"] for p in PART_TYPES}
GROUPS = [g for g in TAXONOMY.groups if any(p["group"] == g for p in PART_TYPES)]  # 대상이 있는 대분류(화면 거르기)
CODE_TO_NAME = {p["code"]: p["name"] for p in PART_TYPES}
DOOR_PART_CODES = [p["code"] for p in PART_TYPES if p["unit"] == "door"]
STATION_PART_CODES = [p["code"] for p in PART_TYPES if p["unit"] == "station"]

# 선로(양식의 "승강장 선로" 칸: 상본선(#3), 하선(#1), 상부본선(#4) ...). 처음엔 상·하 본선만 두고, 기록에 부본선이 나오면
# 그 선로의 문을 자동으로 만든다(데이터셋_v3_설계.md 4장 — 역마다 어느 선로가 있는지는 양식의 숨김 시트에만 있어 쓰지 않는다)
SIDES = {"U": "상선", "D": "하선", "UB": "상부본선", "DB": "하부본선"}
MAIN_SIDES = ["U", "D"]
# 선로당 4량 × 4문 = 16개(임시값, 역별 실제 문 수는 멘토 확인 대기)
DOORS_PER_SIDE = 16
CARS_PER_SIDE = 4

# ---------- 양식의 조치사항·장애인지·장애형태 ----------
# 조치사항(양식 '장애부위 기입' 시트의 조치사항 목록과 같은 순서). 코드는 예전 값과 겹치게 골랐다(full = 교체 = 새 수명)
ACTIONS = {
    "full": "교체",  # 주요 부품을 교체
    "repair": "수리",  # 부품 등을 수리
    "adjust": "조정",  # 부품 등을 조정
    "partial": "단순교체",  # 부속 부품을 교체
    "reset": "리셋",  # 설정값 변경·전원 재인가·커넥터 재결선 등
    "other": "기타",  # 이물질에 의한 일시 장애 등
}
ACTION_ALIASES = {"주요부품교체": "full", "부품교체": "full", "부속교체": "partial", "재결선": "reset"}
# 장애형태는 조치로 정해진다(양식 규칙): 교체 → 장애, 수리·조정·단순교체 → 보수, 리셋·기타 → 예방보수
FAULT_TYPE = {"full": "장애", "repair": "보수", "adjust": "보수", "partial": "보수", "reset": "예방보수", "other": "예방보수"}
# 장애인지: 사후조치(신고받고 출동) = 고장, 사전조치(점검 중 발견·예방) = 예방
REASONS = {"fail": "사후조치", "prev": "사전조치"}
