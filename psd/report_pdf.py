"""역 1곳의 PSD 부품 교체 현황 보고서(PDF, A4).

양식은 목업/보고서양식/PSD_역보고서_태화강_예시.pdf와 같다: 1쪽은 역 개요·상태 요약·조치 필요
부품·부품별 요약·최근 교체 이력, 2쪽은 안전문별 다음 교체 예정일 표. 화면의 역 상세에서 내려받는다.

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
from psd.status import RANK, UnitStatus

BLUE = colors.HexColor("#0065b3")
RED = colors.HexColor("#d0281e")
AMBER = colors.HexColor("#a66f00")
GRAY = colors.HexColor("#666666")
MUTED = colors.HexColor("#555555")
HEAD_BG = colors.HexColor("#f2f4f6")
BORDER = colors.HexColor("#bfc6ce")
FILL = {"빨강": colors.HexColor("#fbd9d6"), "확인_필요": colors.HexColor("#e3e6ea"), "노랑": colors.HexColor("#fdeec4")}
STATE = {"빨강": ("위험", RED), "확인_필요": ("확인 필요", GRAY), "노랑": ("주의", AMBER), "초록": ("안전", BLUE)}
REASON = {"fail": "고장", "prev": "예방 교체", "with": "함께 교체"}
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


def _left(days: int) -> str:
    if days < 0:
        return f"{-days}일 지남"
    m = round(days / 30.44)
    return f"{m // 12}년 {m % 12}개월" if m >= 12 else f"{m}개월" if m else f"{days}일"


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
) -> bytes:
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

    door_label = {d.id: d.label for d in doors}
    loc = lambda u: door_label.get(u.door_id, "역 공통")  # noqa: E731
    counts = {k: sum(1 for u in statuses if u.status == k) for k in STATE}
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
    title = [P("동해선 승강장안전문(PSD) 정비", ps(8.5, bold, BLUE)),
             P(f"{escape(station_name)}역 PSD 부품 교체 현황 보고서", ps(17, bold, lead=23)),
             P(f"기준일 {today:%Y. %m. %d.} · 작성: PSD 교체주기 관리 시스템(자동 생성)", small)]
    head = Table([[title, sign]], colWidths=[CONTENT_W - 66 * mm, 66 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (0, 0), 0), ("RIGHTPADDING", (1, 0), (1, 0), 0)]))
    story += [HRFlowable(width="100%", thickness=3, color=BLUE, spaceAfter=3 * mm), head]

    # 1. 역 개요
    story += section("1. 역 개요")
    n_doors = len(doors)
    info = grid_table([
        [P("역명", ps(9, bold)), P(f"{escape(station_name)}역 (동해선 {station_id}번째 역)"), P("PSD 사용개시", ps(9, bold)), P(f"{psd_install_date:%Y. %m. %d.}")],
        [P("설비 수량", ps(9, bold)), P(f"안전문 {n_doors}개 (방향별 {n_doors // 2}개, 임시값) · 종합제어반 1식"), P("관리 부품", ps(9, bold)), P(f"{len(statuses):,}개 (문별 {len(DOOR_CODES)}종 + 역 공통 {len(STATION_CODES)}종)")],
    ], [22 * mm, CONTENT_W * 0.56 - 22 * mm, 25 * mm, CONTENT_W * 0.44 - 25 * mm], header=False,
        extra=[("BACKGROUND", (0, 0), (0, -1), HEAD_BG), ("BACKGROUND", (2, 0), (2, -1), HEAD_BG), ("ALIGN", (0, 0), (-1, -1), "LEFT")])
    story.append(info)

    # 2. 상태 요약
    story += section("2. 상태 요약")
    kpis = [(counts["빨강"], RED, "위험 · 6개월 안"), (counts["확인_필요"], GRAY, "확인 필요 · 기록 없음"),
            (counts["노랑"], AMBER, "주의 · 1년 안"), (counts["초록"], BLUE, "안전"), (record_total, colors.black, "누적 교체 기록")]
    kpi = Table([[[P(f"{n:,}", ps(16, bold, c, lead=20, align=1)), P(label, small_c)] for n, c, label in kpis]], colWidths=[CONTENT_W / 5] * 5)
    kpi.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, BORDER), ("LINEAFTER", (0, 0), (-2, 0), 0.6, BORDER),
                             ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story.append(kpi)

    # 3. 조치 필요 부품: 현장에 들고 갈 수 있게 "조치 내용"·"확인" 칸은 비워 둔다
    story += section("3. 조치 필요 부품", "위험·확인 필요·주의, 급한 순")
    action = sorted((u for u in statuses if u.status != "초록"), key=lambda u: (RANK[u.status], u.days_left))
    if action:
        rows = [["상태", "위치", "부품", "교체 예정일", "남은 기간", "조치 내용", "확인"]]
        rows += [[P(STATE[u.status][0], ps(8.5, bold, STATE[u.status][1], align=1)), loc(u), NAME[u.part_type_code],
                  _ymd(u.due), _left(u.days_left), "", ""] for u in action]
        story.append(grid_table(rows, [17 * mm, 32 * mm, 30 * mm, 24 * mm, 22 * mm, CONTENT_W - 140 * mm, 15 * mm],
                                extra=[("ALIGN", (1, 1), (2, -1), "LEFT")]))
    else:
        first = min(statuses, key=lambda u: u.days_left)
        same = [u for u in statuses if u.due == first.due and u.part_type_code == first.part_type_code]
        box = Table([[[P("<font name='%s'>기준일 현재 조치가 필요한 부품이 없습니다.</font> (위험·확인 필요·주의 0건)" % bold),
                       P(f"가장 빠른 교체 예정: {NAME[first.part_type_code]} {len(same)}개 · {_ymd(first.due)} ({_left(first.days_left)} 남음)")]]],
                    colWidths=[CONTENT_W])
        box.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, BORDER), ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f7f8fa")),
                                 ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LEFTPADDING", (0, 0), (-1, -1), 8)]))
        story.append(box)

    # 4. 부품별 요약
    story += section("4. 부품별 요약")
    rows = [["부품", "수량", "권장 주기", "가장 빠른 예정일", "남은 기간", "위험", "확인", "주의"]]
    extra = [("ALIGN", (0, 1), (0, -1), "LEFT")]
    for code in DOOR_CODES + STATION_CODES:
        us = [u for u in statuses if u.part_type_code == code]
        if not us:
            continue
        first = min(us, key=lambda u: u.days_left)
        k = lambda s: sum(1 for u in us if u.status == s) or "-"  # noqa: E731
        rows.append([NAME[code], len(us), f"{us[0].cycle.base_years:.1f}년", _ymd(first.due), _left(first.days_left), k("빨강"), k("확인_필요"), k("노랑")])
        if k("빨강") != "-":
            extra += [("TEXTCOLOR", (5, len(rows) - 1), (5, len(rows) - 1), RED), ("FONTNAME", (5, len(rows) - 1), (5, len(rows) - 1), bold)]
    story.append(grid_table(rows, [38 * mm, 16 * mm, 22 * mm, 32 * mm, 26 * mm] + [(CONTENT_W - 134 * mm) / 3] * 3, extra=extra))

    # 5. 최근 교체 이력
    story += section("5. 최근 교체 이력", f"최근 {len(records)}건 / 전체 {record_total:,}건")
    rows = [["교체일", "위치", "부품", "사유", "입력"]]
    rows += [[_ymd(r.replaced_on), r.location, P(escape(", ".join(NAME[c] for c in r.part_type_codes)), ps(8.5)),
              REASON.get(r.reason, r.reason), r.created_by or "-"] for r in records] or [["교체 기록 없음", "", "", "", ""]]
    story.append(grid_table(rows, [28 * mm, 40 * mm, CONTENT_W - 128 * mm, 24 * mm, 36 * mm],
                            extra=[("ALIGN", (1, 1), (2, -1), "LEFT")] + ([("SPAN", (0, 1), (-1, 1))] if not records else [])))

    # 6. 안전문별 다음 교체 예정일(2쪽)
    story += [PageBreak()] + section("6. 안전문별 다음 교체 예정일", "칸 = 해당 부품의 다음 교체 예정일(YY.MM.DD)")
    by_door: dict[int, dict[str, UnitStatus]] = {}
    for u in statuses:
        if u.door_id is not None:
            by_door.setdefault(u.door_id, {})[u.part_type_code] = u
    for side, side_label in SIDES.items():
        side_doors = sorted((d for d in doors if d.side == side), key=lambda d: (d.car, d.position))
        rows = [["문"] + [NAME[c] for c in DOOR_CODES]]
        extra = [("FONTNAME", (0, 1), (0, -1), bold), ("BACKGROUND", (0, 1), (0, -1), colors.HexColor("#f7f8fa")),
                 ("TOPPADDING", (0, 0), (-1, -1), 1.8), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.8)]
        for d in side_doors:
            units = by_door.get(d.id, {})
            rows.append([f"{d.car}-{d.position}"] + [_ymd(units[c].due, short=True) if c in units else "-" for c in DOOR_CODES])
            for col, c in enumerate(DOOR_CODES, start=1):
                if c in units and units[c].status in FILL:
                    extra += [("BACKGROUND", (col, len(rows) - 1), (col, len(rows) - 1), FILL[units[c].status]),
                              ("FONTNAME", (col, len(rows) - 1), (col, len(rows) - 1), bold)]
        story += [P(side_label, ps(9, bold)), Spacer(1, 1 * mm),
                  grid_table(rows, [16 * mm] + [(CONTENT_W - 16 * mm) / len(DOOR_CODES)] * len(DOOR_CODES), extra=extra), Spacer(1, 2.5 * mm)]
    legend = "칸 색:  " + "   ".join(f"<font color='{FILL[k].hexval().replace('0x', '#')}'>■</font> {STATE[k][0]}" for k in FILL) + "   □ 안전"
    story.append(P(legend, small))
    for u in statuses:
        if u.door_id is None:
            story += [Spacer(1, 1.5 * mm), P(f"역 공통 · {NAME[u.part_type_code]}: 다음 교체 {_ymd(u.due)} ({_left(u.days_left)}{' 남음' if u.days_left >= 0 else ''}, {STATE[u.status][0]})")]
    story += [Spacer(1, 3 * mm), HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#cccccc"), spaceAfter=2 * mm)]
    for note in [
        "※ 교체 예정일 = 마지막 교체일(기록이 없으면 PSD 사용개시일) + 역별 권장 주기. 권장 주기는 제조사 권장 10년을 기준으로 이 역의 기상 조건(기상청 AWS)을 반영해 계산했습니다.",
        "※ 상태 기준: 위험 6개월 이하(지난 경우 포함), 확인 필요 = 교체 기록 없이 예정일이 지남, 주의 1년 이하, 안전 그 외.",
        "※ 안전문 수(역당 32개)와 PSD 사용개시일은 확인 전 임시값입니다.",
    ]:
        story.append(P(note, ps(7.8, color=MUTED)))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm, bottomMargin=16 * mm,
                            title=f"{station_name}역 PSD 부품 교체 현황 보고서", author="PSD 교체주기 관리 시스템")
    doc.build(story, canvasmaker=_numbered_canvas(f"{station_name}역 PSD 부품 교체 현황", font))
    return buf.getvalue()
