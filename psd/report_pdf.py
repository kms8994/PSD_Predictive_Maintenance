"""역 1곳의 PSD 부품 교체 현황 보고서(PDF, A4).

받는 사람이 알고 싶은 "어느 안전문의 무엇을 · 몇 개 · 언제까지"가 1쪽에 바로 보이게 짰다(2026-11-02 다시 짬,
처음 양식은 목업/보고서양식/PSD_역보고서_태화강_예시.pdf). 1쪽: 한눈에(상태별 개수·다음 교체), 교체 작업
목록(부품·예정일별로 묶고 대상 문 나열, 완료일 칸), 자재 주문 수량(기간별 + 예비품 참고), 부품별 다음 교체.
2쪽: 안전문별 다음 교체 예정일 표, 최근 교체 이력. 화면의 역 상세에서 내려받는다.

reportlab만 쓴다(크롬·GTK 같은 외부 프로그램 없이 어느 PC에서나 돌게). 한글 글꼴은 맑은 고딕이
있으면 그걸, 없으면 reportlab에 내장된 한글 CID 글꼴(HYGothic)을 쓴다.
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from psd.catalog import PART_TYPES, SIDES
from psd.report_common import WINDOWS, DoorPos, doors_text, left_text, order_counts, work_groups
from psd.status import UnitStatus

BLUE = colors.HexColor("#0065b3")
RED = colors.HexColor("#d0281e")
AMBER = colors.HexColor("#a66f00")
GRAY = colors.HexColor("#666666")
MUTED = colors.HexColor("#555555")
HEAD_BG = colors.HexColor("#f2f4f6")
BORDER = colors.HexColor("#bfc6ce")
FILL = {"빨강": colors.HexColor("#fbd9d6"), "확인_필요": colors.HexColor("#e3e6ea"), "노랑": colors.HexColor("#fdeec4")}
STATE = {"빨강": ("교체 임박", RED), "확인_필요": ("확인 필요", GRAY), "노랑": ("주의", AMBER), "초록": ("안전", BLUE)}
REASON = {"fail": "고장", "prev": "예방 교체", "with": "함께 교체", "check": "점검(이상 없음)"}
NAME = {p["code"]: p["name"] for p in PART_TYPES}
DOOR_CODES = [p["code"] for p in PART_TYPES if p["unit"] == "door"]
STATION_CODES = [p["code"] for p in PART_TYPES if p["unit"] == "station"]
CONTENT_W = A4[0] - 28 * mm


@dataclass(frozen=True)
class DoorInfo:
    id: int
    side: str  # "U" | "D"
    car: int
    position: int
    label: str


@dataclass(frozen=True)
class RecordInfo:
    replaced_on: date
    location: str
    part_type_codes: list[str]
    reason: str
    created_by: str | None


@lru_cache
def _fonts() -> tuple[str, str]:
    """(본문, 굵게) 글꼴 이름. 맑은 고딕 → 나눔고딕 → 내장 CID 순으로 찾는다."""
    font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for regular, bold in [("malgun.ttf", "malgunbd.ttf"), ("NanumGothic.ttf", "NanumGothicBold.ttf")]:
        if (font_dir / regular).exists():
            pdfmetrics.registerFont(TTFont("KR", str(font_dir / regular)))
            has_bold = (font_dir / bold).exists()
            pdfmetrics.registerFont(TTFont("KR-B", str(font_dir / (bold if has_bold else regular))))
            return "KR", "KR-B"
    pdfmetrics.registerFont(UnicodeCIDFont("HYGothic-Medium"))
    return "HYGothic-Medium", "HYGothic-Medium"



def _ymd(d: date, short: bool = False) -> str:
    return d.strftime("%y.%m.%d" if short else "%Y.%m.%d")


def _numbered_canvas(footer: str, font: str):
    """쪽 번호를 "n / 전체"로 찍으려면 전체 쪽 수를 알아야 해서, 쪽을 모았다가 저장할 때 한꺼번에 찍는다."""

    class NumberedCanvas(canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pages = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self.setFont(font, 8)
                self.setFillColor(colors.HexColor("#777777"))
                self.drawCentredString(A4[0] / 2, 9 * mm, f"{footer} · {self._pageNumber} / {total}")
                super().showPage()
            super().save()

    return NumberedCanvas


def build_station_pdf(
    *,
    station_id: int,
    station_name: str,
    psd_install_date: date,
    statuses: list[UnitStatus],
    doors: list[DoorInfo],
    records: list[RecordInfo],
    record_total: int,
    today: date | None = None,
    recent_failures: dict[str, int] | None = None,
) -> bytes:
    """recent_failures: 부품 코드 → 이 역에서 지난 1년 고장으로 바꾼 수(예비품 참고, 없으면 0)."""
    today = today or date.today()
    font, bold = _fonts()

    def ps(size=9, f=None, color=colors.black, lead=None, align=0):
        return ParagraphStyle("p", fontName=f or font, fontSize=size, leading=lead or size * 1.35, textColor=color, alignment=align)

    body, body_c, small, small_c = ps(9), ps(9, align=1), ps(8, color=MUTED), ps(8, color=MUTED, align=1)
    th = ps(8.5, bold, align=1)
    P = lambda text, style=body: Paragraph(text, style)  # noqa: E731

    def grid_table(rows, widths, extra=(), header=True):
        t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
        style = [
            ("GRID", (0, 0), (-1, -1), 0.6, BORDER),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("FONTNAME", (0, 0), (-1, -1), font),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ]
        if header:
            style += [("BACKGROUND", (0, 0), (-1, 0), HEAD_BG), ("FONTNAME", (0, 0), (-1, 0), bold)]
        t.setStyle(TableStyle(style + list(extra)))
        return t

    def section(title, sub=""):
        t = Table([[P(f"<font name='{bold}' size='11'>{escape(title)}</font>"
                      + (f"  <font size='8' color='#666666'>{escape(sub)}</font>" if sub else ""), ps(11))]],
                  colWidths=[CONTENT_W])
        t.setStyle(TableStyle([("LINEBEFORE", (0, 0), (0, 0), 3.5, BLUE), ("LEFTPADDING", (0, 0), (0, 0), 7),
                               ("TOPPADDING", (0, 0), (0, 0), 1), ("BOTTOMPADDING", (0, 0), (0, 0), 1)]))
        return [Spacer(1, 4.5 * mm), t, Spacer(1, 2 * mm)]

    pos = {d.id: DoorPos(d.side, d.car, d.position) for d in doors}
    fails = recent_failures or {}
    counts = {k: sum(1 for u in statuses if u.status == k) for k in STATE}
    groups = work_groups(statuses, pos)
    story = []

    # 머리글 + 결재란
    sign = Table([["결\n재", "담당", "검토", "승인"], ["", "", "", ""]],
                 colWidths=[7 * mm, 19 * mm, 19 * mm, 19 * mm], rowHeights=[6 * mm, 15 * mm])
    sign.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.8, colors.HexColor("#444444")), ("SPAN", (0, 0), (0, 1)),
        ("BACKGROUND", (0, 0), (-1, 0), HEAD_BG), ("BACKGROUND", (0, 0), (0, 1), HEAD_BG),
        ("FONTNAME", (0, 0), (-1, -1), bold), ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    n_doors = len(doors)
    title = [P("동해선 승강장안전문(PSD) 정비", ps(8.5, bold, BLUE)),
             P(f"{escape(station_name)}역 PSD 부품 교체 현황 보고서", ps(17, bold, lead=23)),
             P(f"기준일 {today:%Y. %m. %d.} · 동해선 {station_id}번째 역 · PSD 사용개시 {psd_install_date:%Y. %m. %d.}<br/>"
               f"안전문 {n_doors}개(방향별 {n_doors // 2}개) · 종합제어반 1식 · 관리 부품 {len(statuses):,}개", small)]
    head = Table([[title, sign]], colWidths=[CONTENT_W - 66 * mm, 66 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (0, 0), 0), ("RIGHTPADDING", (1, 0), (1, 0), 0)]))
    story += [HRFlowable(width="100%", thickness=3, color=BLUE, spaceAfter=3 * mm), head, Spacer(1, 3 * mm)]

    # 한눈에: 상태별 개수 + 다음 교체(가장 급한 묶음)
    if groups:
        g0 = groups[0]
        nxt_text = f"{NAME[g0.part_type_code]} {g0.count}개 · {_ymd(g0.due)}<br/>{left_text(g0.days_left)}"
    else:
        first = min(statuses, key=lambda u: u.days_left)
        nxt_text = f"{NAME[first.part_type_code]} · {_ymd(first.due)}<br/>{left_text(first.days_left)}"
    kpis = [(f"{counts['빨강']:,}", RED, "교체 임박<br/>6개월 안"), (f"{counts['확인_필요']:,}", GRAY, "확인 필요<br/>기록 없이 지남"),
            (f"{counts['노랑']:,}", AMBER, "주의<br/>1년 안"), (f"{counts['초록']:,}", BLUE, "안전<br/>1년 넘게 남음")]
    cells = [[P(n, ps(16, bold, c, lead=20, align=1)), P(label, small_c)] for n, c, label in kpis]
    cells.append([P("다음 교체", small_c), P(nxt_text, ps(9.5, bold, align=1, lead=13))])
    kpi = Table([cells], colWidths=[CONTENT_W * 0.16] * 4 + [CONTENT_W * 0.36])
    kpi.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, BORDER), ("LINEAFTER", (0, 0), (-2, 0), 0.6, BORDER),
                             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                             ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story.append(kpi)

    # 1. 교체 작업 목록: 같은 부품·예정일·상태인 문을 한 줄로 묶고 문 번호를 나열한다(예전엔 문마다 한 줄이라
    #    IFM 12개가 12줄로 나와 무엇을 몇 개 바꾸는지 세어야 했다). "완료일"은 현장에서 적는 칸
    story += section("1. 교체 작업 목록", "1년 안에 교체할 부품과 확인 필요, 급한 순 · 완료일은 작업 후 기입")
    if groups:
        rows = [["상태", "부품", "수량", "교체 예정일", "남은 기간", "대상 안전문", "완료일"]]
        rows += [[P(STATE[g.status][0], ps(8.5, bold, STATE[g.status][1], align=1)), NAME[g.part_type_code], f"{g.count}개",
                  _ymd(g.due), left_text(g.days_left), P("<br/>".join(escape(t) for t in doors_text(g, "\n").split("\n")), ps(8.5)), ""]
                 for g in groups]
        story.append(grid_table(rows, [16 * mm, 24 * mm, 12 * mm, 20 * mm, 20 * mm, CONTENT_W - 110 * mm, 18 * mm],
                                extra=[("ALIGN", (5, 1), (5, -1), "LEFT")]))
    else:
        first = min(statuses, key=lambda u: u.days_left)
        same = [u for u in statuses if u.due == first.due and u.part_type_code == first.part_type_code]
        box = Table([[[P("<font name='%s'>1년 안에 교체할 부품이 없습니다.</font> (교체 임박·확인 필요·주의 0건)" % bold),
                       P(f"가장 빠른 교체 예정: {NAME[first.part_type_code]} {len(same)}개 · {_ymd(first.due)} ({left_text(first.days_left)})")]]],
                    colWidths=[CONTENT_W])
        box.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, BORDER), ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f7f8fa")),
                                 ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LEFTPADDING", (0, 0), (-1, -1), 8)]))
        story.append(box)

    # 2. 자재 주문 수량: 부품마다 기간별 교체 개수 + 돌발 고장 예비품 참고
    story += section("2. 자재 주문 수량", "예정일이 그 기간에 오는 부품 수 · 예비품은 지난 1년 고장 수 참고")
    oc = order_counts(statuses)
    rows = [["부품", WINDOWS[0][1], WINDOWS[1][1], "1년 안 합계", WINDOWS[2][1], "확인 필요", P("지난 1년 고장<br/>(예비품 참고)", th)]]
    tot = [0] * 6
    for code, n in oc.items():
        vals = [n["6m"], n["1y"], n["6m"] + n["1y"], n["2y"], n["check"], fails.get(code, 0)]
        tot = [a + b for a, b in zip(tot, vals)]
        rows.append([NAME[code], *[v or "-" for v in vals]])
    rows.append(["합계", *tot])
    w = (CONTENT_W - 38 * mm) / 6
    story.append(grid_table(rows, [38 * mm] + [w] * 6, extra=[
        ("ALIGN", (0, 1), (0, -1), "LEFT"), ("FONTNAME", (0, -1), (-1, -1), bold), ("BACKGROUND", (0, -1), (-1, -1), HEAD_BG),
        ("FONTNAME", (3, 1), (3, -1), bold), ("BACKGROUND", (3, 1), (3, -2), colors.HexColor("#f7f8fa"))]))

    # 3. 부품별 다음 교체: 부품 종류마다 교체주기와 가장 빠른 예정일
    story += section("3. 부품별 다음 교체", "교체주기 = 이 역 날씨·고장 기록으로 보정한 값")
    rows = [["부품", "수량", "교체주기", "가장 빠른 교체", "남은 기간", "교체 임박", "주의", "안전"]]
    extra = [("ALIGN", (0, 1), (0, -1), "LEFT")]
    for code in DOOR_CODES + STATION_CODES:
        us = [u for u in statuses if u.part_type_code == code]
        if not us:
            continue
        first = min(us, key=lambda u: u.days_left)
        k = lambda st: sum(1 for u in us if u.status == st) or "-"  # noqa: E731
        rows.append([NAME[code], len(us), f"{us[0].cycle_years:.1f}년", _ymd(first.due), left_text(first.days_left), k("빨강"), k("노랑"), k("초록")])
        if k("빨강") != "-":
            extra += [("TEXTCOLOR", (5, len(rows) - 1), (5, len(rows) - 1), RED), ("FONTNAME", (5, len(rows) - 1), (5, len(rows) - 1), bold)]
    story.append(grid_table(rows, [38 * mm, 15 * mm, 20 * mm, 27 * mm, 27 * mm] + [(CONTENT_W - 127 * mm) / 3] * 3, extra=extra))

    story += [Spacer(1, 4 * mm), HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#cccccc"), spaceAfter=2 * mm)]
    for note in [
        "※ 교체 예정일 = 마지막 교체일(기록이 없으면 PSD 사용개시일) + 이 역의 교체주기. 교체주기는 제조사 권장 10년을 이 역 날씨(기상청 AWS)와 고장 기록으로 보정했습니다.",
        "※ 상태 기준: 교체 임박 6개월 이하(지난 경우 포함), 확인 필요 = 교체 기록 없이 예정일이 지남(교체 여부부터 확인), 주의 1년 이하, 안전 그 외.",
        "※ 자재 주문 수량은 예정 교체만 셉니다. 돌발 고장 대비 예비품은 '지난 1년 고장'을 참고하세요. 안전문 수(역당 32개)와 PSD 사용개시일은 확인 전 임시값입니다.",
    ]:
        story.append(P(note, ps(7.6, color=MUTED)))

    # 4. 안전문별 다음 교체 예정일(2쪽): 문 하나하나의 부품별 예정일
    story += [PageBreak()] + section("4. 안전문별 다음 교체 예정일", "칸 = 그 부품의 다음 교체 예정일(YY.MM.DD), 색 = 상태")
    by_door: dict[int, dict[str, UnitStatus]] = {}
    for u in statuses:
        if u.door_id is not None:
            by_door.setdefault(u.door_id, {})[u.part_type_code] = u
    for side, side_label in SIDES.items():
        side_doors = sorted((d for d in doors if d.side == side), key=lambda d: (d.car, d.position))
        rows = [["문"] + [NAME[c] for c in DOOR_CODES]]
        extra = [("FONTNAME", (0, 1), (0, -1), bold), ("BACKGROUND", (0, 1), (0, -1), colors.HexColor("#f7f8fa")),
                 ("TOPPADDING", (0, 0), (-1, -1), 1.3), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.3)]
        for d in side_doors:
            units = by_door.get(d.id, {})
            rows.append([f"{d.car}-{d.position}"] + [_ymd(units[c].due, short=True) if c in units else "-" for c in DOOR_CODES])
            for col, c in enumerate(DOOR_CODES, start=1):
                if c in units and units[c].status in FILL:
                    extra += [("BACKGROUND", (col, len(rows) - 1), (col, len(rows) - 1), FILL[units[c].status]),
                              ("FONTNAME", (col, len(rows) - 1), (col, len(rows) - 1), bold)]
        story += [P(side_label, ps(9, bold)), Spacer(1, 1 * mm),
                  grid_table(rows, [16 * mm] + [(CONTENT_W - 16 * mm) / len(DOOR_CODES)] * len(DOOR_CODES), extra=extra), Spacer(1, 2 * mm)]
    legend = "칸 색:  " + "   ".join(f"<font color='{FILL[k].hexval().replace('0x', '#')}'>■</font> {STATE[k][0]}" for k in FILL) + "   □ 안전"
    story.append(P(legend, small))
    for u in statuses:
        if u.door_id is None:
            story += [Spacer(1, 1 * mm), P(f"역 공통 · {NAME[u.part_type_code]}: 다음 교체 {_ymd(u.due)} ({left_text(u.days_left)}, {STATE[u.status][0]})")]

    # 5. 최근 교체 이력
    story += section("5. 최근 교체 이력", f"최근 {len(records)}건 / 전체 {record_total:,}건")
    rows = [["교체일", "위치", "부품", "사유", "입력"]]
    rows += [[_ymd(r.replaced_on), r.location, P(escape(", ".join(NAME[c] for c in r.part_type_codes)), ps(8.5)),
              REASON.get(r.reason, r.reason), r.created_by or "-"] for r in records] or [["교체 기록 없음", "", "", "", ""]]
    story.append(grid_table(rows, [24 * mm, 36 * mm, CONTENT_W - 118 * mm, 24 * mm, 34 * mm],
                            extra=[("ALIGN", (1, 1), (2, -1), "LEFT"), ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]
                            + ([("SPAN", (0, 1), (-1, 1))] if not records else [])))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm, bottomMargin=16 * mm,
                            title=f"{station_name}역 PSD 부품 교체 현황 보고서", author="PSD 교체주기 관리 시스템")
    doc.build(story, canvasmaker=_numbered_canvas(f"{station_name}역 PSD 부품 교체 현황", font))
    return buf.getvalue()
