"""정비 기록 엑셀 입출력 — 코레일 "승강장안전문 장애 이력 관리 현황" 양식 (2026-11-24 v3, 데이터셋_v3_설계.md 1·5장).

현장이 쓰는 양식 파일을 그대로 읽는다:
- 시트: "고장현황 전체"가 있으면 그것, 없으면 첫 시트
- 머리글 행: 위에서 40줄 안에서 "장애부위②" 칸이 있는 줄(양식은 12행, 우리 내려받기 양식은 1행)
- 년·월·일·역이 모두 빈 줄은 건너뛴다(양식은 수식만 깔린 빈 줄이 1만 줄쯤 있다). 양식의 '일자' 칸(수식)은 읽지 않는다
- 노선이 동해선이 아닌 줄은 건너뛰고 몇 줄인지 알려 준다(양식은 전 노선 공통)

읽는 칸(머리글 이름. 띄어쓰기·줄바꿈은 무시하고, 양식 '작성기준' 시트의 다른 이름도 받는다):
    년도·월·일, 역, 장애인지, 장애부위②(소분류), 조치사항 — 꼭 있어야 한다
    승강장 선로·출입문 — 문 단위 소분류면 꼭 있어야 한다(예: "상본선(#3)", "2-1")
    나머지(본부·노선·PSD설치·설치년도·제조업체·제어시스템·DCU(MCU)업체·관할사업소·장애형태·승강장 홈·장애부위①·
    장애내용①②·고장원인·조치사항 상세·접수자·접수일·완료일·설치주체·조치자)는 비워도 된다

틀리면 멈추는 것(ExcelFormatError): 날짜, 역 이름 빈 칸, 모르는 장애인지·소분류·조치사항, 문 단위인데 선로·출입문이 없거나 형식이 틀림.
경고만 하고 받는 것(ParsedRow.warnings): 장애형태가 조치와 맞지 않음, 대분류가 소분류와 맞지 않음, 분류표에 없는 장애내용①,
출입문이 엑셀에서 날짜로 바뀐 것("2-1" → 2월 1일)을 되돌린 것. 현장 파일이 조금 흐트러져도 한 줄 때문에 전부 막지 않으려고.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from io import BytesIO

from psd.catalog import ACTION_ALIASES, ACTIONS, FAULT_TYPE, GROUPS, PART_TYPES, REASONS, SIDES, TAXONOMY
from psd.stations import LINE_NAME
from psd.taxonomy import normalize

SHEET_NAME = "고장현황 전체"
# 양식 '고장현황 전체' 시트의 칸 순서('일자' 수식 칸은 뺌). 내려받기도 이 순서로 쓴다
COLUMNS = ["년도", "월", "일", "본부", "노선", "역", "PSD설치", "설치년도", "제조업체", "제어시스템", "DCU(MCU)업체", "관할사업소",
           "장애형태", "장애인지", "승강장 선로", "승강장 홈", "출입문", "장애부위①(대분류)", "장애부위②(소분류)", "장애내용①",
           "장애내용②(상세)", "고장원인", "조치사항", "조치사항 상세", "접수자", "접수일", "완료일", "설치주체", "조치자"]
REQUIRED = ["년도", "월", "일", "역", "장애인지", "장애부위②(소분류)", "조치사항"]
# 같은 칸의 다른 이름(양식 '작성기준' 시트, 줄여 쓴 것)
_ALIASES = {"DCU업체": "DCU(MCU)업체", "장애원인": "고장원인", "조치사항(상세)": "조치사항 상세", "조치사항상세": "조치사항 상세",
            "장애부위①": "장애부위①(대분류)", "장애부위②": "장애부위②(소분류)", "장애내용②": "장애내용②(상세)",
            "선로": "승강장 선로", "홈": "승강장 홈", "승강장홈": "승강장 홈", "승강장선로": "승강장 선로", "연도": "년도",
            "장애부위1": "장애부위①(대분류)", "장애부위2": "장애부위②(소분류)", "장애내용1": "장애내용①", "장애내용2": "장애내용②(상세)"}
_HEADER_KEY = {**{re.sub(r"\s", "", c): c for c in COLUMNS}, **{re.sub(r"\s", "", k): v for k, v in _ALIASES.items()}}

REASON_LABEL_TO_CODE = {v: k for k, v in REASONS.items()} | {"사후": "fail", "사전": "prev"}
REASON_CODE_TO_LABEL = dict(REASONS)
ACTION_LABEL_TO_CODE = {v: k for k, v in ACTIONS.items()}
UNIT_BY_CODE = {p["code"]: p for p in PART_TYPES}
_DOOR_NO = re.compile(r"^(\d+)-(\d+)$")
_TRACK = re.compile(r"^(상|하)(부)?(본)?(선|행)?(\(?#?\d+\)?)?$")


class ExcelFormatError(Exception):
    """행 단위 파싱 에러. row_no는 엑셀 기준 줄 번호(0이면 파일 전체 문제)."""

    def __init__(self, row_no: int, message: str):
        self.row_no = row_no
        super().__init__(f"{row_no}행: {message}" if row_no else message)


@dataclass
class ParsedRow:
    row_no: int
    station_name: str
    replaced_on: date  # 발생일(년·월·일)
    reason_code: str  # 장애인지: fail | prev
    unit_code: str  # 장애부위②(소분류) 코드
    action_code: str  # 조치사항 코드(catalog.ACTIONS)
    side: str | None = None  # 선로 코드(catalog.SIDES), 역 단위 소분류면 None
    door_no: str | None = None  # "차량-문", 예: "1-1". 역 단위 소분류면 None
    track_raw: str | None = None
    home: str | None = None
    symptom: str | None = None
    symptom_detail: str | None = None
    cause_text: str | None = None
    action_detail: str | None = None
    receiver: str | None = None
    received: str | None = None
    completed: str | None = None
    created_by: str | None = None  # 조치자
    station_info: dict = field(default_factory=dict)  # 역 정보 칸(본부·노선·PSD설치·설치년도·업체 ...)
    warnings: list[str] = field(default_factory=list)

    @property
    def part_names(self) -> list[str]:
        return [UNIT_BY_CODE[self.unit_code]["name"]]


@dataclass
class ParseResult:
    rows: list[ParsedRow]
    skipped_other_line: int = 0  # 동해선이 아니라 건너뛴 줄
    sheet: str = ""
    header_row: int = 0

    @property
    def warnings(self) -> list[str]:
        return [f"{r.row_no}행: {w}" for r in self.rows for w in r.warnings]


def parse_track(text: str) -> str | None:
    """승강장 선로 글 → 선로 코드. "상본선(#3)"·"하선(#1)"·"상부본선(#4)"·"상"·"하행" 등. 못 읽으면 None."""
    m = _TRACK.match(re.sub(r"\s", "", text or ""))
    if not m:
        return None
    return ("U" if m.group(1) == "상" else "D") + ("B" if m.group(2) else "")


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _int(v) -> int | None:
    t = _text(v)
    if not t:
        return None
    try:
        return int(float(t))
    except ValueError:
        return None


def _open_sheet(content: bytes):
    from openpyxl import load_workbook

    try:
        wb = load_workbook(BytesIO(content), read_only=True, data_only=True)
    except Exception:
        raise ExcelFormatError(0, "엑셀 파일을 읽을 수 없습니다. .xlsx 파일인지, 다른 프로그램에서 열려 있거나 손상되지 않았는지 확인하세요")
    return wb[SHEET_NAME] if SHEET_NAME in wb.sheetnames else wb.worksheets[0]


def parse_upload(content: bytes) -> ParseResult:
    """엑셀 바이트를 읽어 행마다 검사한다. 형식이 틀린 행이 있으면 전부 모아 ExcelFormatError 하나로 낸다
    (역 이름이 실제 있는지, 그 문이 있는지 같은 DB 검사는 psd/api.py가 한다)."""
    ws = _open_sheet(content)
    rows_iter = ws.iter_rows(values_only=True)
    header_row, cols = 0, {}
    for i, row in enumerate(rows_iter, start=1):
        keys = {_HEADER_KEY.get(re.sub(r"\s", "", _text(v))): j for j, v in enumerate(row or ()) if _text(v)}
        if "장애부위②(소분류)" in keys:
            header_row, cols = i, {k: j for k, j in keys.items() if k}
            break
        if i >= 40:
            break
    if not header_row:
        raise ExcelFormatError(0, f"머리글 줄을 찾지 못했습니다 — '{SHEET_NAME}' 시트 위쪽 40줄 안에 '장애부위②(소분류)' 칸이 있어야 합니다")
    missing = [c for c in REQUIRED if c not in cols]
    if missing:
        raise ExcelFormatError(header_row, f"필요한 칸이 없습니다: {missing}")

    out = ParseResult([], sheet=ws.title, header_row=header_row)
    errors: list[str] = []
    for row_no, row in enumerate(rows_iter, start=header_row + 1):
        get = lambda c: row[cols[c]] if c in cols and cols[c] < len(row) else None  # noqa: E731
        if not any(_text(get(c)) for c in ("년도", "월", "일", "역")):
            continue  # 빈 줄(양식의 수식만 있는 줄 포함)
        line = _text(get("노선"))
        if line and normalize(line) != normalize(LINE_NAME):
            out.skipped_other_line += 1
            continue
        try:
            out.rows.append(_parse_row(row_no, get))
        except ExcelFormatError as e:
            errors.append(str(e))
    if errors:
        raise ExcelFormatError(0, f"{len(errors)}개 줄이 틀렸습니다 — " + " / ".join(errors[:30]) + (" ..." if len(errors) > 30 else ""))
    if not out.rows:
        raise ExcelFormatError(0, "읽을 기록이 한 줄도 없습니다" + (f"(동해선이 아닌 줄 {out.skipped_other_line}개는 건너뜀)" if out.skipped_other_line else ""))
    return out


def _parse_row(row_no: int, get) -> ParsedRow:
    warnings: list[str] = []
    y, m, d = _int(get("년도")), _int(get("월")), _int(get("일"))
    try:
        day = date(y, m, d)
    except (TypeError, ValueError):
        raise ExcelFormatError(row_no, f"년·월·일을 날짜로 읽을 수 없습니다: {_text(get('년도'))}-{_text(get('월'))}-{_text(get('일'))}")
    if day > date.today():
        raise ExcelFormatError(row_no, f"발생일은 미래일 수 없습니다: {day}")

    station = _text(get("역"))
    if not station:
        raise ExcelFormatError(row_no, "역 이름이 비어 있습니다")

    reason_label = _text(get("장애인지"))
    reason = REASON_LABEL_TO_CODE.get(re.sub(r"\s", "", reason_label))
    if reason is None:
        raise ExcelFormatError(row_no, f"장애인지는 {list(REASONS.values())} 중 하나여야 합니다: {reason_label!r}")

    unit_text = _text(get("장애부위②(소분류)"))
    code = TAXONOMY.resolve(unit_text)
    if code is None:
        raise ExcelFormatError(row_no, f"분류표에 없는 장애부위②(소분류): {unit_text!r}")
    unit = UNIT_BY_CODE[code]
    group_text = _text(get("장애부위①(대분류)"))
    if group_text and TAXONOMY.resolve_group(group_text) != unit["group"]:
        warnings.append(f"대분류 {group_text!r}가 소분류 {unit['name']!r}의 대분류({unit['group']})와 다름 — 소분류를 따름")

    action_text = _text(get("조치사항"))
    action = ACTION_LABEL_TO_CODE.get(action_text) or ACTION_ALIASES.get(re.sub(r"\s", "", action_text))
    if action is None:
        raise ExcelFormatError(row_no, f"조치사항은 {list(ACTIONS.values())} 중 하나여야 합니다: {action_text!r}")
    fault = _text(get("장애형태"))
    if fault and re.sub(r"\s", "", fault) != FAULT_TYPE[action]:
        warnings.append(f"장애형태 {fault!r}가 조치사항 {ACTIONS[action]!r}의 규칙({FAULT_TYPE[action]})과 다름 — 조치사항을 따름")

    symptom = _text(get("장애내용①")) or None
    if symptom and unit["symptoms"] and normalize(symptom) not in {normalize(s) for s in unit["symptoms"]}:
        warnings.append(f"장애내용① {symptom!r}는 {unit['name']}의 목록에 없음 — 그대로 받음")

    track_raw = _text(get("승강장 선로")) or None
    door_raw = get("출입문")
    if isinstance(door_raw, (date, datetime)):  # 엑셀이 "2-1"을 2월 1일로 바꿔 버린 경우
        door_no = f"{door_raw.month}-{door_raw.day}"
        warnings.append(f"출입문이 날짜({door_raw:%m-%d})로 저장돼 있어 {door_no!r}로 읽음 — 엑셀에서 텍스트로 바꿔 두세요")
    else:
        door_no = _text(door_raw) or None
    side = None
    if unit["unit"] == "door":
        side = parse_track(track_raw or "")
        if side is None:
            raise ExcelFormatError(row_no, f"{unit['name']}은(는) 문 단위라 승강장 선로가 필요합니다(예: '상본선(#3)', '하선'): {track_raw!r}")
        if not door_no or not _DOOR_NO.match(door_no):
            raise ExcelFormatError(row_no, f"{unit['name']}은(는) 문 단위라 출입문이 '차량-문' 형식이어야 합니다(예: '2-1'): {door_no!r}")
    else:
        door_no = None  # 역 단위 소분류는 문을 쓰지 않는다(선로 글은 그대로 남긴다)

    info = {"hq": _text(get("본부")) or None, "line_name": _text(get("노선")) or None, "office": _text(get("관할사업소")) or None,
            "structure": (_text(get("PSD설치")) or None), "install_year": _int(get("설치년도")),
            "manufacturer": _text(get("제조업체")) or None, "control_system": _text(get("제어시스템")) or None,
            "dcu_vendor": _text(get("DCU(MCU)업체")) or None, "install_owner": _text(get("설치주체")) or None}
    cut = lambda c, n: (_text(get(c))[:n] or None)  # noqa: E731
    return ParsedRow(
        row_no=row_no, station_name=station, replaced_on=day, reason_code=reason, unit_code=code, action_code=action,
        side=side, door_no=door_no, track_raw=(track_raw or "")[:30] or None, home=cut("승강장 홈", 10),
        symptom=(symptom or "")[:50] or None, symptom_detail=cut("장애내용②(상세)", 500), cause_text=cut("고장원인", 500),
        action_detail=cut("조치사항 상세", 500), receiver=cut("접수자", 50), received=cut("접수일", 50), completed=cut("완료일", 50),
        created_by=cut("조치자", 50), station_info={k: v for k, v in info.items() if v is not None}, warnings=warnings,
    )


# ---------- 내려받기 ----------


def record_row(*, day: date, station: str, reason: str, unit_code: str, action: str, side: str | None = None,
               door_no: str | None = None, track_raw: str | None = None, home: str | None = None, symptom: str | None = None,
               symptom_detail: str | None = None, cause_text: str | None = None, action_detail: str | None = None,
               receiver: str | None = None, received: str | None = None, completed: str | None = None,
               worker: str | None = None, station_info: dict | None = None) -> dict:
    """기록 하나 → 양식 한 줄(COLUMNS 키). 내려받기·더미 생성이 같은 표기를 쓰게 한다."""
    unit = UNIT_BY_CODE[unit_code]
    info = station_info or {}
    return {
        "년도": day.year, "월": day.month, "일": day.day, "본부": info.get("hq") or "", "노선": info.get("line_name") or LINE_NAME,
        "역": station, "PSD설치": info.get("structure") or "", "설치년도": info.get("install_year") or "",
        "제조업체": info.get("manufacturer") or "", "제어시스템": info.get("control_system") or "",
        "DCU(MCU)업체": info.get("dcu_vendor") or "", "관할사업소": info.get("office") or "",
        "장애형태": FAULT_TYPE[action], "장애인지": REASONS[reason],
        "승강장 선로": track_raw or (SIDES[side] if side else ""), "승강장 홈": home or "", "출입문": door_no or "",
        "장애부위①(대분류)": unit["group"], "장애부위②(소분류)": unit["name"], "장애내용①": symptom or "",
        "장애내용②(상세)": symptom_detail or "", "고장원인": cause_text or "", "조치사항": ACTIONS[action],
        "조치사항 상세": action_detail or "", "접수자": receiver or "", "접수일": received or "", "완료일": completed or "",
        "설치주체": info.get("install_owner") or "", "조치자": worker or "",
    }


def _write_sheet(ws, rows: list[dict]) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill

    ws.append(COLUMNS)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1D5EA8")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for r in rows:
        ws.append([r.get(c, "") for c in COLUMNS])
    for cell in ws["Q"][1:]:  # 출입문: 엑셀이 "2-1"을 날짜로 바꾸지 않게 텍스트 칸으로
        cell.number_format = "@"
    ws.freeze_panes = "A2"
    widths = {"역": 10, "승강장 선로": 12, "장애부위②(소분류)": 16, "장애내용①": 16, "장애내용②(상세)": 24, "고장원인": 24,
              "조치사항 상세": 24}
    from openpyxl.utils import get_column_letter

    for i, c in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(c, 9)


def _add_lists(wb, ws, n_rows: int) -> None:
    """드롭다운: 장애인지·장애부위①②·조치사항·장애형태. 목록은 셋째 시트 '목록'(보이는 시트)에 둔다 —
    소분류 이름에 쉼표가 있어 칸 안에 목록을 바로 적을 수 없다."""
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    lists = {"장애인지": list(REASONS.values()), "장애부위①(대분류)": GROUPS, "장애부위②(소분류)": [p["name"] for p in PART_TYPES],
             "조치사항": list(ACTIONS.values()), "장애형태": sorted(set(FAULT_TYPE.values()))}
    ls = wb.create_sheet("목록")
    last = max(n_rows + 1, 2000)
    for j, (col, values) in enumerate(lists.items(), start=1):
        letter = get_column_letter(j)
        ls.cell(1, j, col)
        for i, v in enumerate(values, start=2):
            ls.cell(i, j, v)
        dv = DataValidation(type="list", formula1=f"=목록!${letter}$2:${letter}${len(values) + 1}", allow_blank=True)
        dv.error, dv.showErrorMessage = "목록에서 고르세요", False  # 막지는 않는다(업로드가 검사한다)
        target = get_column_letter(COLUMNS.index(col) + 1)
        dv.add(f"{target}2:{target}{last}")
        ws.add_data_validation(dv)


def rows_to_excel_bytes(rows: list[dict]) -> bytes:
    """기록 줄(record_row 결과)을 양식 칸 순서의 엑셀로. 첫 시트 이름은 양식과 같은 "고장현황 전체"."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    _write_sheet(ws, rows)
    _add_lists(wb, ws, len(rows))
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def template_bytes(examples: list[dict]) -> bytes:
    """업로드 양식: 첫 시트 "고장현황 전체"는 머리글만, 둘째 시트 "작성 예시"에 예시. 업로드는 "고장현황 전체"만 읽어서
    예시를 안 지워도 가짜 기록이 들어가지 않는다. 현장이 이미 쓰는 코레일 양식 파일을 그대로 올려도 된다."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    _write_sheet(ws, [])
    _write_sheet(wb.create_sheet("작성 예시"), examples)
    _add_lists(wb, ws, 0)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
