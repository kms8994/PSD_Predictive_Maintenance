"""교체(고장) 기록 엑셀 입출력.

업로드 API(psd/api.py의 POST /api/replacement-records/upload)와 더미데이터 생성
(scripts/generate_dummy_records.py)이 같은 열 구성을 쓰게 한다 — 설계_v2.md 5-2
"교체 기록 입력 항목"(교체일, 역, 문, 부품(여러 개), 사유)과 같은 항목이다.

열: 역, 방향, 문번호, 교체일, 사유, 부품, 입력자
- 방향·문번호: 역 단위 부품(종합제어반 릴레이)만 교체했으면 비움
- 방향: "부전 방면" | "북울산 방면" (psd/catalog.py SIDES의 값)
- 문번호: "차량-문" 형식, 예: "1-1"
- 사유: "고장" | "예방 교체" | "함께 교체" (psd/schemas.py VALID_REASONS의 한글 값과 같다)
- 부품: 쉼표로 여러 개, 예: "DCM,IFM 릴레이" (psd/catalog.py PART_TYPES의 이름)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from io import BytesIO

import pandas as pd

from psd.catalog import NAME_TO_CODE, SIDES

COLUMNS = ["역", "방향", "문번호", "교체일", "사유", "부품", "입력자"]
REASON_LABEL_TO_CODE = {"고장": "fail", "예방 교체": "prev", "함께 교체": "with"}
REASON_CODE_TO_LABEL = {v: k for k, v in REASON_LABEL_TO_CODE.items()}
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
    replaced_on: date
    reason_code: str
    part_names: list[str]
    created_by: str | None


def _cell(row: pd.Series, col: str) -> str:
    v = row.get(col)
    return str(v).strip() if pd.notna(v) else ""


def parse_upload(content: bytes) -> list[ParsedRow]:
    """엑셀 바이트를 읽어 행마다 검사한다. 형식이 잘못되면 ExcelFormatError를 낸다
    (DB 조회가 필요한 검사 — 역 이름이 실제 있는지, 그 문에 그 부품이 있는지 등 — 는
    psd/api.py가 담당한다. 여기서는 엑셀 자체의 형식만 본다)."""
    try:
        df = pd.read_excel(BytesIO(content), dtype=str)
    except Exception as e:
        raise ExcelFormatError(0, f"엑셀 파일을 읽을 수 없습니다: {e}")

    missing_cols = set(COLUMNS) - set(df.columns)
    if missing_cols:
        raise ExcelFormatError(0, f"필요한 열이 없습니다: {sorted(missing_cols)} (필요한 열: {COLUMNS})")
    if df.empty:
        raise ExcelFormatError(0, "행이 하나도 없습니다")

    rows = []
    for i, r in df.iterrows():
        row_no = i + 2  # 1행은 헤더

        station_name = _cell(r, "역")
        if not station_name:
            raise ExcelFormatError(row_no, "역 이름이 비어 있습니다")

        replaced_on_raw = r.get("교체일")
        try:
            replaced_on = pd.to_datetime(replaced_on_raw).date()
        except Exception:
            raise ExcelFormatError(row_no, f"교체일을 날짜로 읽을 수 없습니다: {replaced_on_raw!r}")

        reason_label = _cell(r, "사유")
        if reason_label not in REASON_LABEL_TO_CODE:
            raise ExcelFormatError(row_no, f"사유는 {list(REASON_LABEL_TO_CODE)} 중 하나여야 합니다: {reason_label!r}")

        parts_raw = _cell(r, "부품")
        part_names = [p.strip() for p in parts_raw.split(",") if p.strip()]
        if not part_names:
            raise ExcelFormatError(row_no, "부품이 비어 있습니다")
        unknown = [p for p in part_names if p not in NAME_TO_CODE]
        if unknown:
            raise ExcelFormatError(row_no, f"알 수 없는 부품 이름: {unknown} (가능한 값: {sorted(NAME_TO_CODE)})")

        side_label = _cell(r, "방향") or None
        door_no = _cell(r, "문번호") or None
        if side_label and side_label not in SIDE_LABEL_TO_CODE:
            raise ExcelFormatError(row_no, f"방향은 {list(SIDE_LABEL_TO_CODE)} 중 하나이거나 비어 있어야 합니다: {side_label!r}")
        if bool(side_label) != bool(door_no):
            raise ExcelFormatError(row_no, "방향과 문번호는 둘 다 채우거나 둘 다 비워야 합니다")

        rows.append(ParsedRow(
            row_no=row_no,
            station_name=station_name,
            side_label=side_label,
            door_no=door_no,
            replaced_on=replaced_on,
            reason_code=REASON_LABEL_TO_CODE[reason_label],
            part_names=part_names,
            created_by=_cell(r, "입력자") or None,
        ))
    return rows


def rows_to_excel_bytes(rows: list[dict]) -> bytes:
    """더미데이터 생성 스크립트가 쓴다. rows는 COLUMNS와 같은 키를 가진 dict의 리스트."""
    df = pd.DataFrame(rows, columns=COLUMNS)
    buf = BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()
