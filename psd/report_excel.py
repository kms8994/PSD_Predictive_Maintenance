"""PSD 교체 보고서(엑셀). 역 하나 또는 전체 역 (2026-11-02 다시 짬).

예전 보고서는 부품 하나가 한 줄(역 하나에 194줄, 전체 4,400줄)이고 주기 범위·날씨 판정 같은 계산 값이 섞여
있어, 받는 사람이 "무엇을 몇 개 언제까지"를 직접 세어야 했다. 시트를 쓰임새대로 나눈다.

1. 요약: 상태별 개수, 자재 주문 수량(부품 x 기간 + 예비품 참고), 읽는 법
2. 교체 작업: 1년 안 교체할 것(+ 확인 필요)을 부품·예정일별로 묶고 대상 안전문을 나열. 완료일·비고 칸은 비워 둔다
3. 역별 주문(전체 역 보고서만): 역 x 부품 기간별 개수 — 역마다 자재를 나눠 보낼 때
4. 전체 부품: 부품 하나하나(예전 보고서와 같은 단위). 거르기·정렬용
"""
from __future__ import annotations

from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from psd.catalog import SIDES
from psd.report_common import NAME, PART_ORDER, STATUS_LABEL, WINDOWS, DoorPos, doors_text, left_text, order_counts, work_groups
from psd.status import UnitStatus

# 화면 .pill 색과 맞춘다(psd/web/index.html)
FILL = {"빨강": "FBE8E4", "노랑": "FBF1DC", "초록": "E2F3E9", "확인_필요": "ECEFF3"}
FONT = {"빨강": "CF4430", "노랑": "C98A12", "초록": "2B9457", "확인_필요": "7D8796"}
HEAD = PatternFill("solid", fgColor="1D5EA8")
SUB = PatternFill("solid", fgColor="EEF2F7")
THIN = Border(*(Side(style="thin", color="C9D1DB"),) * 4)


def _header(ws, row: int, values: list[str]) -> None:
    for col, v in enumerate(values, start=1):
        c = ws.cell(row=row, column=col, value=v)
        c.font, c.fill, c.border = Font(bold=True, color="FFFFFF"), HEAD, THIN
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _widths(ws, widths: list[float]) -> None:
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _status_cell(cell, status: str) -> None:
    cell.value = STATUS_LABEL[status]
    cell.fill = PatternFill("solid", fgColor=FILL[status])
    cell.font = Font(bold=True, color=FONT[status])
    cell.alignment = Alignment(horizontal="center")


def build_report_workbook(
    statuses: list[UnitStatus],
    station_names: dict[int, str],
    doors: dict[int, DoorPos],
    install_dates: dict[int, date],
    recent_failures: dict[tuple[int, str], int],
    title: str,
    today: date | None = None,
) -> Workbook:
    """install_dates: 역 id → PSD 사용개시일(교체 기록이 없는 부품의 기준일). recent_failures: (역, 부품) → 지난 1년 고장 교체 수."""
    today = today or date.today()
    multi = len({u.station_id for u in statuses}) > 1
    wb = Workbook()

    # ---------- 1. 요약 ----------
    ws = wb.active
    ws.title = "요약"
    ws["A1"] = title
    ws["A1"].font = Font(bold=True, size=15)
    n_doors = len({u.door_id for u in statuses if u.door_id})
    ws["A2"] = f"기준일 {today:%Y.%m.%d} · 안전문 {n_doors:,}개 · 관리 부품 {len(statuses):,}개"
    ws["A2"].font = Font(color="666666")

    counts = {k: sum(1 for u in statuses if u.status == k) for k in STATUS_LABEL}
    _header(ws, 4, ["교체 임박(6개월 안)", "확인 필요", "주의(1년 안)", "안전"])
    for col, k in enumerate(STATUS_LABEL, start=1):
        c = ws.cell(row=5, column=col, value=counts[k])
        c.font, c.alignment, c.border = Font(bold=True, size=13, color=FONT[k]), Alignment(horizontal="center"), THIN

    ws["A7"] = "자재 주문 수량"
    ws["A7"].font = Font(bold=True, size=12)
    ws["A8"] = "예정일이 그 기간에 오는 부품 수입니다. 돌발 고장은 언제 날지 몰라 빠져 있으니 예비품은 오른쪽 '지난 1년 고장'을 참고하세요."
    ws["A8"].font = Font(color="666666")
    head = ["부품", *[label for _k, label, _d in WINDOWS[:2]], "1년 안 합계", WINDOWS[2][1], "확인 필요", "지난 1년 고장(예비품 참고)"]
    _header(ws, 9, head)
    oc = order_counts(statuses)
    fails_by_code = {code: sum(n for (sid, c), n in recent_failures.items() if c == code) for code in PART_ORDER}
    r = 10
    totals = [0] * 6
    for code, n in oc.items():
        vals = [n["6m"], n["1y"], n["6m"] + n["1y"], n["2y"], n["check"], fails_by_code.get(code, 0)]
        totals = [a + b for a, b in zip(totals, vals)]
        ws.cell(row=r, column=1, value=NAME[code]).border = THIN
        for col, v in enumerate(vals, start=2):
            c = ws.cell(row=r, column=col, value=v or None)
            c.border, c.alignment = THIN, Alignment(horizontal="center")
            if col == 4:
                c.font, c.fill = Font(bold=True), SUB
        r += 1
    ws.cell(row=r, column=1, value="합계").font = Font(bold=True)
    for col, v in enumerate(totals, start=2):
        c = ws.cell(row=r, column=col, value=v)
        c.font, c.border, c.alignment = Font(bold=True), THIN, Alignment(horizontal="center")

    notes = [
        "읽는 법",
        "· 교체 임박 = 예정일까지 6개월 이하(이미 지난 것 포함), 주의 = 1년 이하, 확인 필요 = 교체 기록 없이 예정일이 지남(바꿨는지부터 확인).",
        "· 예정일 = 마지막 교체일(기록이 없으면 PSD 사용개시일) + 그 역·부품의 교체주기. 교체주기는 제조사 권장 10년을 역 날씨와 고장 기록으로 보정한 값입니다.",
        "· '교체 작업' 시트에 어느 안전문의 무엇을 언제까지 바꿀지 묶어 두었습니다. 완료일·비고 칸에 적어 현장에서 쓰세요.",
    ]
    for i, t in enumerate(notes):
        c = ws.cell(row=r + 2 + i, column=1, value=t)
        c.font = Font(bold=True) if i == 0 else Font(color="555555")
    _widths(ws, [20, 14, 14, 14, 14, 12, 24])

    # ---------- 2. 교체 작업 ----------
    ws = wb.create_sheet("교체 작업")
    cols = (["역"] if multi else []) + ["상태", "부품", "수량", "교체 예정일", "남은 기간", "대상 안전문", "완료일", "비고"]
    _header(ws, 1, cols)
    groups = work_groups(statuses, doors)
    for i, g in enumerate(groups, start=2):
        vals = ([station_names[g.station_id]] if multi else []) + [None, NAME[g.part_type_code], g.count, g.due, left_text(g.days_left), doors_text(g), None, None]
        for col, v in enumerate(vals, start=1):
            c = ws.cell(row=i, column=col, value=v)
            c.border = THIN
            c.alignment = Alignment(vertical="top", wrap_text=cols[col - 1] == "대상 안전문")
        _status_cell(ws.cell(row=i, column=cols.index("상태") + 1), g.status)
        ws.cell(row=i, column=cols.index("교체 예정일") + 1).number_format = "yyyy-mm-dd"
        ws.cell(row=i, column=cols.index("수량") + 1).alignment = Alignment(horizontal="center", vertical="top")
    if not groups:
        ws.cell(row=2, column=1, value="1년 안에 교체할 부품이 없습니다.")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    _widths(ws, ([10] if multi else []) + [11, 16, 7, 13, 13, 52, 12, 20])

    # ---------- 3. 역별 주문(전체 역) ----------
    if multi:
        ws = wb.create_sheet("역별 주문")
        _header(ws, 1, ["역", "부품", WINDOWS[0][1], WINDOWS[1][1], "1년 안 합계", WINDOWS[2][1], "확인 필요", "지난 1년 고장"])
        for sid in sorted(station_names):
            st = [u for u in statuses if u.station_id == sid]
            for code, n in order_counts(st).items():
                vals = [n["6m"], n["1y"], n["6m"] + n["1y"], n["2y"], n["check"], recent_failures.get((sid, code), 0)]
                if not any(vals):
                    continue
                ws.append([station_names[sid], NAME[code], *[v or None for v in vals]])
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        _widths(ws, [10, 16, 11, 11, 12, 11, 10, 12])

    # ---------- 4. 전체 부품 ----------
    ws = wb.create_sheet("전체 부품")
    cols = ["역", "방향", "문", "부품", "상태", "다음 교체일", "남은 일수", "남은 기간", "교체주기(년)", "마지막 교체일", "기준"]
    _header(ws, 1, cols)

    def sort_key(u: UnitStatus):
        d = doors.get(u.door_id) if u.door_id else None
        return (u.station_id, d is None, d.side != "U" if d else 0, d.car if d else 0, d.position if d else 0, PART_ORDER.index(u.part_type_code))

    for i, u in enumerate(sorted(statuses, key=sort_key), start=2):
        d = doors.get(u.door_id) if u.door_id else None
        replaced = u.install_date != install_dates.get(u.station_id)
        ws.append([station_names[u.station_id], SIDES[d.side] if d else "역 공통", d.no if d else "종합제어반", NAME[u.part_type_code], None,
                   u.due, u.days_left, left_text(u.days_left), round(u.cycle_years, 1), u.install_date,
                   "마지막 교체일" if replaced else "PSD 사용개시일(교체 기록 없음)"])
        _status_cell(ws.cell(row=i, column=5), u.status)
        for col in (6, 10):
            ws.cell(row=i, column=col).number_format = "yyyy-mm-dd"
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    _widths(ws, [10, 12, 11, 16, 11, 12, 9, 13, 11, 13, 26])
    return wb
