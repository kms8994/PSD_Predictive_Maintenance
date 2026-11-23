"""정비(교체·수리·점검) 기록 엑셀 입출력.

업로드 API(psd/api.py의 POST /api/replacement-records/compare·sync)와 더미데이터 생성
(scripts/generate_dummy_records.py)이 같은 열 구성을 쓰게 한다 — 설계_v2.md 5-2
"교체 기록 입력 항목"(교체일, 역, 문, 부품(여러 개), 사유)에 2026-11-16 조치·세부 부품·증상·원인을 더했다
(데이터셋_v2_설계.md 1장).

열: 역, 방향, 문번호, 작업일, 사유, 조치, 부품, 세부 부품, 증상, 원인, 입력자
- 방향·문번호: 역 단위 부품(종합제어반 릴레이)만 손봤으면 비움
- 방향: "부전 방면" | "북울산 방면" (psd/catalog.py SIDES의 값)
- 문번호: "차량-문" 형식, 예: "1-1"
- 작업일: 교체·수리·점검한 날. 예전 양식의 "교체일" 열도 같은 뜻으로 받는다
- 사유(왜 했나): "고장" | "예방 정비" | "함께 정비" | "점검(이상 없음)". 예전 표기("예방 교체", "함께 교체")도 받는다.
  점검(이상 없음)은 정기 점검에서 문제가 없었다는 기록이다 — 부품을 손본 게 아니라서 설치일은 그대로다.
  "점검", "이상 없음"으로 써도 같은 뜻으로 받는다
- 조치(무엇을 했나): "전체 교체" | "일부 교체" | "전체 수리" | "일부 수리" | "없음"(점검만). 비우거나 열이 없으면
  점검은 없음, 나머지는 전체 교체로 읽는다(예전 양식 그대로 올려도 되게). "조정"·"청소"는 일부 수리로 받는다
- 부품: 쉼표로 여러 개, 예: "DCM,IFM 릴레이" (psd/catalog.py PART_TYPES의 이름).
  "전체"라고 쓰면 그 문의 부품 6종 전부(방향·문번호를 비웠으면 종합제어반 릴레이) — 문 단위 점검용
- 세부 부품: 일부 교체·일부 수리에만, 쉼표로 여러 개(psd/catalog.py SUB_PARTS). 일부 교체는 꼭 적는다.
  그 줄의 부품에 딸린 것이어야 한다
- 증상·원인: 고장일 때만, 하나씩(psd/catalog.py SYMPTOMS·CAUSES). 비워도 된다
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from io import BytesIO

import pandas as pd

from psd.catalog import (
    ACTION_ALIASES,
    ACTIONS,
    CAUSE_ALIASES,
    CAUSE_NAME_TO_CODE,
    CAUSES,
    DOOR_PART_CODES,
    NAME_TO_CODE,
    SIDES,
    STATION_PART_CODES,
    SUB_PART_ACTIONS,
    SUB_PART_CODE_TO_NAME,
    SUB_PART_NAME_TO_CODE,
    SUB_PART_OF,
    SYMPTOM_NAME_TO_CODE,
    SYMPTOMS,
)

COLUMNS = ["역", "방향", "문번호", "작업일", "사유", "조치", "부품", "세부 부품", "증상", "원인", "입력자"]
REQUIRED_COLUMNS = ["역", "방향", "문번호", "작업일", "사유", "부품", "입력자"]  # 조치·세부 부품·증상·원인은 없어도 된다(예전 양식)
OLD_DATE_COLUMN = "교체일"  # 2026-11-16 전 양식의 날짜 열 이름
REASON_LABEL_TO_CODE = {"고장": "fail", "예방 정비": "prev", "함께 정비": "with", "점검(이상 없음)": "check"}
REASON_CODE_TO_LABEL = {v: k for k, v in REASON_LABEL_TO_CODE.items()}
REASON_ALIASES = {"점검": "check", "이상 없음": "check", "점검 이상 없음": "check",  # 손으로 쓸 때 흔한 표기
                  "예방 교체": "prev", "함께 교체": "with", "예방": "prev", "함께": "with"}  # 2026-11-16 전 표기
ACTION_LABEL_TO_CODE = {v: k for k, v in ACTIONS.items()}
ALL_PARTS = "전체"
CODE_TO_NAME = {code: name for name, code in NAME_TO_CODE.items()}
SIDE_LABEL_TO_CODE = {label: code for code, label in SIDES.items()}


class ExcelFormatError(Exception):
    """행 단위 파싱 에러. row_no는 엑셀 기준 줄 번호(1행=헤더)."""

    def __init__(self, row_no: int, message: str):
        self.row_no = row_no
        super().__init__(f"{row_no}행: {message}")


@dataclass
class ParsedRow:
    row_no: int
    station_name: str
    side_label: str | None  # None이면 역 단위 부품
    door_no: str | None  # "차량-문", 예: "1-1"
    replaced_on: date  # 작업일
    reason_code: str
    part_names: list[str]
    created_by: str | None
    action_code: str = "full"
    sub_part_codes: tuple[str, ...] = ()  # 정렬된 세부 부품 코드
    symptom_code: str | None = None
    cause_code: str | None = None


def _cell(row: pd.Series, col: str) -> str:
    v = row.get(col)
    return str(v).strip() if pd.notna(v) else ""


def parse_upload(content: bytes) -> list[ParsedRow]:
    """엑셀 바이트를 읽어 행마다 검사한다. 형식이 잘못되면 ExcelFormatError를 낸다
    (DB 조회가 필요한 검사 — 역 이름이 실제 있는지, 그 문에 그 부품이 있는지 등 — 는
    psd/api.py가 담당한다. 여기서는 엑셀 자체의 형식만 본다). 첫 시트만 읽는다(양식의 "작성 예시" 시트는 무시)."""
    try:
        df = pd.read_excel(BytesIO(content), sheet_name=0, dtype=str)
    except Exception:
        # pandas 오류 문구(영어, 예: "Excel file format cannot be determined")를 그대로 보여 주면 현장에서 알아보기 어렵다
        raise ExcelFormatError(0, "엑셀 파일을 읽을 수 없습니다. .xlsx 파일인지, 다른 프로그램에서 열려 있거나 손상되지 않았는지 확인하세요")

    if "작업일" not in df.columns and OLD_DATE_COLUMN in df.columns:
        df = df.rename(columns={OLD_DATE_COLUMN: "작업일"})
    missing_cols = set(REQUIRED_COLUMNS) - set(df.columns)
    if missing_cols:
        raise ExcelFormatError(0, f"필요한 열이 없습니다: {sorted(missing_cols)} (필요한 열: {REQUIRED_COLUMNS}, "
                                  "더 적을 수 있는 열: 조치·세부 부품·증상·원인)")
    if df.empty:
        raise ExcelFormatError(0, "행이 하나도 없습니다")

    rows = []
    for i, r in df.iterrows():
        row_no = i + 2  # 1행은 헤더

        station_name = _cell(r, "역")
        if not station_name:
            raise ExcelFormatError(row_no, "역 이름이 비어 있습니다")

        replaced_on_raw = r.get("작업일")
        # 빈 칸은 pd.to_datetime이 에러 대신 NaT를 돌려줘 아래 검사를 모두 통과하고 DB에 넣을 때 500으로 죽었다(2026-11-02 QA)
        if pd.isna(replaced_on_raw) or not str(replaced_on_raw).strip():
            raise ExcelFormatError(row_no, "작업일이 비어 있습니다")
        try:
            replaced_on = pd.to_datetime(replaced_on_raw).date()
        except Exception:
            raise ExcelFormatError(row_no, f"작업일을 날짜로 읽을 수 없습니다: {replaced_on_raw!r}")
        # 코드 리뷰로 찾은 버그: 미래 날짜가 날짜로 읽히기만 하면 통과하고 있었다. 여기서 막는다
        # — 전체가 한 트랜잭션(all-or-nothing)이라 한 행만 틀려도 전부 막힌다.
        if replaced_on > date.today():
            raise ExcelFormatError(row_no, f"작업일은 미래일 수 없습니다: {replaced_on}")

        reason_label = _cell(r, "사유")
        reason_code = REASON_LABEL_TO_CODE.get(reason_label) or REASON_ALIASES.get(reason_label)
        if reason_code is None:
            raise ExcelFormatError(row_no, f"사유는 {list(REASON_LABEL_TO_CODE)} 중 하나여야 합니다: {reason_label!r}")

        side_label = _cell(r, "방향") or None
        door_no = _cell(r, "문번호") or None
        if side_label and side_label not in SIDE_LABEL_TO_CODE:
            raise ExcelFormatError(row_no, f"방향은 {list(SIDE_LABEL_TO_CODE)} 중 하나이거나 비어 있어야 합니다: {side_label!r}")
        if bool(side_label) != bool(door_no):
            raise ExcelFormatError(row_no, "방향과 문번호는 둘 다 채우거나 둘 다 비워야 합니다")

        parts_raw = _cell(r, "부품")
        if parts_raw == ALL_PARTS:
            part_names = [CODE_TO_NAME[c] for c in (DOOR_PART_CODES if side_label else STATION_PART_CODES)]
        else:
            part_names = [p.strip() for p in parts_raw.split(",") if p.strip()]
        if not part_names:
            raise ExcelFormatError(row_no, "부품이 비어 있습니다")
        unknown = [p for p in part_names if p not in NAME_TO_CODE]
        if unknown:
            raise ExcelFormatError(row_no, f"알 수 없는 부품 이름: {unknown} (가능한 값: {sorted(NAME_TO_CODE)} 또는 {ALL_PARTS!r})")

        action_code, sub_codes, symptom_code, cause_code = _detail(row_no, r, reason_code, part_names)

        rows.append(ParsedRow(
            row_no=row_no,
            station_name=station_name,
            side_label=side_label,
            door_no=door_no,
            replaced_on=replaced_on,
            reason_code=reason_code,
            part_names=part_names,
            created_by=_cell(r, "입력자") or None,
            action_code=action_code,
            sub_part_codes=sub_codes,
            symptom_code=symptom_code,
            cause_code=cause_code,
        ))
    return rows


def _detail(row_no: int, r: pd.Series, reason_code: str, part_names: list[str]) -> tuple[str, tuple[str, ...], str | None, str | None]:
    """조치·세부 부품·증상·원인 칸을 읽고 사유·부품과 맞는지 본다."""
    action_label = _cell(r, "조치")
    if not action_label:
        action_code = "none" if reason_code == "check" else "full"
    else:
        action_code = ACTION_LABEL_TO_CODE.get(action_label) or ACTION_ALIASES.get(action_label)
        if action_code is None:
            raise ExcelFormatError(row_no, f"조치는 {list(ACTION_LABEL_TO_CODE)} 중 하나여야 합니다: {action_label!r}")
    if reason_code == "check" and action_code != "none":
        raise ExcelFormatError(row_no, f"점검(이상 없음)인데 조치가 {ACTIONS[action_code]!r}입니다 — 손봤으면 사유를 예방 정비로 적으세요")
    if reason_code != "check" and action_code == "none":
        raise ExcelFormatError(row_no, "조치 '없음'은 점검(이상 없음)에만 씁니다")

    sub_names = [x.strip() for x in _cell(r, "세부 부품").split(",") if x.strip()]
    unknown = [x for x in sub_names if x not in SUB_PART_NAME_TO_CODE]
    if unknown:
        raise ExcelFormatError(row_no, f"알 수 없는 세부 부품: {unknown} (가능한 값: {list(SUB_PART_NAME_TO_CODE)})")
    sub_codes = tuple(sorted({SUB_PART_NAME_TO_CODE[x] for x in sub_names}))
    if sub_codes and action_code not in SUB_PART_ACTIONS:
        raise ExcelFormatError(row_no, f"세부 부품은 일부 교체·일부 수리에만 적습니다(조치: {ACTIONS[action_code]})")
    if action_code == "partial" and not sub_codes:
        raise ExcelFormatError(row_no, "일부 교체는 바꾼 세부 부품을 적어야 합니다")
    part_codes = {NAME_TO_CODE[n] for n in part_names}
    stray = [SUB_PART_CODE_TO_NAME[c] for c in sub_codes if SUB_PART_OF[c] not in part_codes]
    if stray:
        raise ExcelFormatError(row_no, f"세부 부품 {stray}은(는) 이 줄의 부품({', '.join(part_names)})에 딸린 것이 아닙니다")

    symptom_label, cause_label = _cell(r, "증상"), _cell(r, "원인")
    if (symptom_label or cause_label) and reason_code != "fail":
        raise ExcelFormatError(row_no, "증상·원인은 사유가 고장일 때만 적습니다")
    symptom_code = None
    if symptom_label:
        symptom_code = SYMPTOM_NAME_TO_CODE.get(symptom_label)
        if symptom_code is None:
            raise ExcelFormatError(row_no, f"증상은 {list(SYMPTOM_NAME_TO_CODE)} 중 하나여야 합니다: {symptom_label!r}")
    cause_code = None
    if cause_label:
        cause_code = CAUSE_NAME_TO_CODE.get(cause_label) or CAUSE_ALIASES.get(cause_label)
        if cause_code is None:
            raise ExcelFormatError(row_no, f"원인은 {list(CAUSE_NAME_TO_CODE)} 중 하나여야 합니다: {cause_label!r}")
    return action_code, sub_codes, symptom_code, cause_code


def detail_labels(action: str, sub_parts, symptom: str | None, cause: str | None) -> dict[str, str]:
    """코드 → 엑셀 표기(조치·세부 부품·증상·원인 칸). 내려받기·더미 생성이 같은 표기를 쓰게 한다."""
    return {
        "조치": ACTIONS[action],
        "세부 부품": ",".join(SUB_PART_CODE_TO_NAME[c] for c in sub_parts),
        "증상": SYMPTOMS[symptom]["name"] if symptom else "",
        "원인": CAUSES[cause] if cause else "",
    }


def rows_to_excel_bytes(rows: list[dict]) -> bytes:
    """더미데이터 생성·내려받기가 쓴다. rows는 COLUMNS와 같은 키를 가진 dict의 리스트
    (예전 키 "교체일"도 "작업일"로 받고, 없는 칸은 비운다)."""
    rows = [{("작업일" if k == OLD_DATE_COLUMN else k): v for k, v in row.items()} for row in rows]
    df = pd.DataFrame(rows, columns=COLUMNS).fillna("")
    buf = BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def template_bytes(examples: list[dict]) -> bytes:
    """업로드 양식: 첫 시트 "기록"은 열 이름만 두고, 예시는 둘째 시트 "작성 예시"에 둔다 — 예시 행을
    안 지우고 올려도 가짜 기록이 들어가지 않게(업로드는 첫 시트만 읽는다)."""
    buf = BytesIO()
    with pd.ExcelWriter(buf) as w:
        pd.DataFrame(columns=COLUMNS).to_excel(w, sheet_name="기록", index=False)
        pd.DataFrame(examples, columns=COLUMNS).fillna("").to_excel(w, sheet_name="작성 예시", index=False)
    return buf.getvalue()
