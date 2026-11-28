"""역 목록과 역 환경 — 고정 엑셀 파일 읽기 (2026-11-24).

노선은 동해선 하나로 고정한다(LINE_NAME). 역 목록과 역 환경은 한 번 정하면 거의 바뀌지 않아 업로드로 받지 않고 고정 파일에 둔다.

- 역 목록: data/stations/동해선_역목록.xlsx (공개) — ID(영구 번호)·순서·역 이름·옛 이름·개통일·PSD 사용개시일·AWS 지점·운영·
  바다·하천 인접(추정)·비고. 파일 안 '안내' 시트에 고치는 규칙, '변경 이력' 시트에 바꾼 기록이 있다.
  파일을 고친 뒤 `python scripts/init_db.py --check`로 DB와 무엇이 다른지 보고 `python scripts/init_db.py`로 반영한다.
  예전엔 psd/cycle.py에 코드로 적혀 있었다 — 값의 근거(미팅 답변 Q3·Q5 등)는 그 파일의 '변경 이력' 시트로 옮겼다.
- 역 환경: data/private/역환경.xlsx (비공개, 없으면 공개 예시 data/sample/역환경_예시.xlsx — 값 없음). '항목' 시트가
  카테고리·항목·값 종류(선택/숫자/글)를 정하고, '역 환경' 시트에 역(ID)마다 값을 적는다. 화면 표시용이고 아직 계산에는 안 쓴다.
  파일이 바뀌면 다시 읽는다(서버를 다시 켤 필요 없음). 틀린 값은 앱을 멈추지 않고 경고로 보인다.

역 목록이 틀리면(ID·순서·이름 겹침, 날짜·AWS 지점 오류) 불러올 때 바로 멈춘다 — 역이 틀리면 모든 계산이 틀리기 때문이다.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINE_NAME = "동해선"
# PSD_STATION_FILE: 다른 역 목록 파일로 시험해 볼 때(예: 고친 사본을 init_db.py --check로 미리 보기)
STATION_FILE = Path(os.environ.get("PSD_STATION_FILE") or ROOT / "data" / "stations" / "동해선_역목록.xlsx")
ENV_FILE = ROOT / "data" / "private" / "역환경.xlsx"
ENV_SAMPLE = ROOT / "data" / "sample" / "역환경_예시.xlsx"

# 역 → 가장 가까운 AWS(방재기상관측) 지점 코드의 이름. 날씨 자료(scripts/preprocess_aws_weather.py)가 이 지점들만 받는다 —
# 역 목록에 새 지점을 쓰려면 여기에 더하고 날씨를 다시 받아야 한다(그래서 엑셀이 아니라 코드에 둔다)
AWS_STATION_NAMES: dict[str, str] = {
    "160": "부산(레)", "296": "북부산", "904": "사상", "910": "영도", "921": "가덕도",
    "923": "기장", "937": "해운대", "938": "부산진", "939": "금정구", "940": "동래",
    "941": "북구", "942": "부산남구", "950": "사하",
    "854": "삼동", "898": "장생포", "900": "두서", "901": "울기", "924": "간절곶",
    "943": "매곡", "949": "정자", "954": "온산",
}

_STATION_COLS = ["ID", "순서", "역 이름", "옛 이름", "개통일", "PSD 사용개시일", "AWS 지점", "운영", "바다·하천 인접(추정)", "업체", "비고"]
# 업체(2026-11-28, 미팅/코레일_피드백_정리_1127.md): 유지보수 업체. 일광까지 업체 1, 좌천~태화강 업체 2. 부품 목록은 같고 회사만 다르다 —
# 내년 부품 사용량(예산) 계획을 업체별로도 보려고 둔다. 비워도 된다(전체 합계에만 들어간다). 칸이 없는 예전 파일도 읽는다
VENDORS = ("업체 1", "업체 2")


@dataclass(frozen=True)
class Station:
    id: int  # 영구 번호 — 기록·문·부품이 이 번호로 묶인다. 바꾸거나 다시 쓰지 않는다
    seq: int  # 노선 순서(화면·보고서 정렬). 역이 중간에 생기면 이것만 다시 매긴다
    name: str
    open_date: date  # 철도 개통일
    psd_install_date: date | None = None  # PSD 사용개시일. None이면 open_date로 대신한다
    aws_stn: str = ""
    in_service: bool = True  # False(개통 전)면 문·부품을 만들지 않는다 — 날짜가 지나도 자동으로 바뀌지 않는다
    near_water: bool = False  # 바다·하천 인접(추정, 표시용)
    aliases: tuple[str, ...] = ()  # 옛 이름(이 이름으로 적힌 정비기록도 이 역으로 읽는다)
    note: str = ""
    vendor: str = ""  # 유지보수 업체(VENDORS 중 하나, 비면 미정)

    @property
    def psd_start(self) -> date:
        return self.psd_install_date or self.open_date


class StationFileError(ValueError):
    pass


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _date(v, where: str, required: bool) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    t = _text(v)
    if not t:
        if required:
            raise StationFileError(f"{where}: 날짜가 비어 있습니다")
        return None
    try:
        return date.fromisoformat(t[:10])
    except ValueError:
        raise StationFileError(f"{where}: 날짜로 읽을 수 없습니다: {t!r}")


def _rows(ws) -> tuple[list[str], list[tuple[int, dict]]]:
    it = ws.iter_rows(values_only=True)
    head = [_text(h) for h in next(it, ())]
    out = []
    for i, row in enumerate(it, start=2):
        rec = {h: v for h, v in zip(head, row) if h}
        if any(_text(v) for v in rec.values()):
            out.append((i, rec))
    return head, out


def load_station_file(path: Path = STATION_FILE) -> list[Station]:
    """역 목록 파일을 읽어 순서대로 돌려준다. 틀린 곳이 있으면 전부 모아 StationFileError."""
    from openpyxl import load_workbook

    if not path.exists():
        raise StationFileError(f"역 목록 파일이 없습니다: {path}")
    wb = load_workbook(path, read_only=True, data_only=True)
    if "역 목록" not in wb.sheetnames:
        raise StationFileError(f"{path.name}: '역 목록' 시트가 없습니다")
    head, rows = _rows(wb["역 목록"])
    missing = [c for c in _STATION_COLS[:3] + ["개통일", "AWS 지점", "운영"] if c not in head]
    if missing:
        raise StationFileError(f"{path.name} '역 목록' 시트에 칸이 없습니다: {missing}")
    errors, out = [], []
    for i, r in rows:
        where = f"{path.name} {i}행"
        try:
            sid, seq = int(float(_text(r.get("ID")))), int(float(_text(r.get("순서"))))
            name = _text(r.get("역 이름"))
            if not name:
                raise StationFileError(f"{where}: 역 이름이 비어 있습니다")
            aws = _text(r.get("AWS 지점"))
            if aws not in AWS_STATION_NAMES:
                raise StationFileError(f"{where}: AWS 지점 {aws!r}를 모릅니다(쓸 수 있는 지점: psd/stations.py AWS_STATION_NAMES)")
            run = _text(r.get("운영"))
            if run not in ("운영", "개통 전"):
                raise StationFileError(f"{where}: 운영은 '운영' 또는 '개통 전'이어야 합니다: {run!r}")
            near = _text(r.get("바다·하천 인접(추정)"))
            if near not in ("", "예", "아니오"):
                raise StationFileError(f"{where}: 바다·하천 인접은 '예' 또는 '아니오'여야 합니다: {near!r}")
            open_date = _date(r.get("개통일"), f"{where} 개통일", True)
            psd = _date(r.get("PSD 사용개시일"), f"{where} PSD 사용개시일", False)
            if psd and psd < open_date:
                raise StationFileError(f"{where}: PSD 사용개시일({psd})이 개통일({open_date})보다 앞섭니다")
            aliases = tuple(a for a in (x.strip() for x in re.split(r"[,，]", _text(r.get("옛 이름")))) if a)
            vendor = re.sub(r"\s+", " ", _text(r.get("업체"))).replace("업체1", "업체 1").replace("업체2", "업체 2")
            if vendor and vendor not in VENDORS:
                raise StationFileError(f"{where}: 업체는 {list(VENDORS)} 중 하나이거나 비워야 합니다: {vendor!r}")
            out.append(Station(sid, seq, name, open_date, psd, aws, run == "운영", near == "예", aliases, _text(r.get("비고")), vendor))
        except (StationFileError, ValueError) as e:
            errors.append(str(e) if isinstance(e, StationFileError) else f"{where}: ID·순서는 숫자여야 합니다")
    for label, key in (("ID", lambda s: s.id), ("순서", lambda s: s.seq)):
        seen: dict = {}
        for s in out:
            if key(s) in seen:
                errors.append(f"{path.name}: {label} {key(s)}가 {seen[key(s)]}·{s.name}에 겹칩니다")
            seen[key(s)] = s.name
    names: dict[str, str] = {}
    for s in out:
        for n in (s.name, *s.aliases):
            if n in names and names[n] != s.name:
                errors.append(f"{path.name}: 이름 {n!r}가 {names[n]}·{s.name}에 겹칩니다(역 이름·옛 이름)")
            names[n] = s.name
    if errors:
        raise StationFileError("역 목록 파일이 틀렸습니다:\n- " + "\n- ".join(errors))
    if not out:
        raise StationFileError(f"{path.name}: 역이 하나도 없습니다")
    return sorted(out, key=lambda s: s.seq)


STATIONS: list[Station] = load_station_file()
STATION_BY_ID = {s.id: s for s in STATIONS}
# 역 이름(옛 이름 포함) → ID. 정비기록 엑셀의 역 이름을 찾을 때 쓴다
STATION_ID_BY_NAME = {n: s.id for s in STATIONS for n in (s.name, *s.aliases)}


# ---------- 역 환경 ----------


@dataclass(frozen=True)
class EnvField:
    category: str
    name: str  # 항목 이름 = '역 환경' 시트의 열 이름
    kind: str  # "선택" | "숫자" | "글"
    choices: tuple[str, ...] = ()
    unit: str = ""
    desc: str = ""


@dataclass
class Environment:
    source: str  # "private" | "sample" | 파일 경로
    fields: list[EnvField] = field(default_factory=list)
    values: dict[int, dict[str, object]] = field(default_factory=dict)  # 역 ID → {항목 이름: 값}
    warnings: list[str] = field(default_factory=list)

    @property
    def categories(self) -> list[str]:
        return list(dict.fromkeys(f.category for f in self.fields))

    def for_station(self, station_id: int) -> list[dict]:
        """화면용: 카테고리마다 항목·값(없으면 None)."""
        vals = self.values.get(station_id, {})
        return [{"category": c, "items": [{"name": f.name, "kind": f.kind, "unit": f.unit, "desc": f.desc, "value": vals.get(f.name)}
                                          for f in self.fields if f.category == c]} for c in self.categories]


def _env_path() -> tuple[Path, str]:
    env = os.environ.get("PSD_STATION_ENV", "").strip()
    if env == "sample":
        return ENV_SAMPLE, "sample"
    if env:
        return Path(env), env
    return (ENV_FILE, "private") if ENV_FILE.exists() else (ENV_SAMPLE, "sample")


def read_environment(path: Path, source: str) -> Environment:
    """역 환경 파일을 읽는다. 틀린 값은 그 칸만 비우고 warnings에 적는다(표시용이라 앱을 멈추지 않는다)."""
    from openpyxl import load_workbook

    env = Environment(source)
    if not path.exists():
        env.warnings.append(f"역 환경 파일이 없습니다: {path.name}")
        return env
    wb = load_workbook(path, read_only=True, data_only=True)
    for sheet in ("항목", "역 환경"):
        if sheet not in wb.sheetnames:
            env.warnings.append(f"{path.name}: '{sheet}' 시트가 없습니다")
            return env
    _h, frows = _rows(wb["항목"])
    for i, r in frows:
        cat, name, kind = _text(r.get("카테고리")), _text(r.get("항목")), _text(r.get("값 종류"))
        if not (cat and name):
            env.warnings.append(f"{path.name} 항목 {i}행: 카테고리·항목 이름이 비어 있어 뺌")
            continue
        if kind not in ("선택", "숫자", "글"):
            env.warnings.append(f"{path.name} 항목 {i}행: 값 종류 {kind!r}를 몰라 '글'로 봄")
            kind = "글"
        if any(f.name == name for f in env.fields):
            env.warnings.append(f"{path.name} 항목 {i}행: 항목 {name!r}가 겹쳐 뺌")
            continue
        choices = tuple(c.strip() for c in _text(r.get("선택지")).split(",") if c.strip())
        if kind == "선택" and not choices:
            env.warnings.append(f"{path.name} 항목 {i}행: {name}은 '선택'인데 선택지가 없어 '글'로 봄")
            kind = "글"
        env.fields.append(EnvField(cat, name, kind, choices, _text(r.get("단위")), _text(r.get("설명"))))

    head, vrows = _rows(wb["역 환경"])
    by_name = {f.name: f for f in env.fields}
    for col in head:
        if col and col not in ("ID", "역 이름") and col not in by_name:
            env.warnings.append(f"{path.name} 역 환경: '{col}' 열이 '항목' 시트에 없어 읽지 않음")
    for f in env.fields:
        if f.name not in head:
            env.warnings.append(f"{path.name} 역 환경: 항목 '{f.name}'의 열이 없음(모든 역이 빈 값)")
    for i, r in vrows:
        try:
            sid = int(float(_text(r.get("ID"))))
        except ValueError:
            env.warnings.append(f"{path.name} 역 환경 {i}행: ID가 숫자가 아니라 뺌")
            continue
        st = STATION_BY_ID.get(sid)
        if st is None:
            env.warnings.append(f"{path.name} 역 환경 {i}행: 역 목록에 없는 ID {sid}라 뺌")
            continue
        shown = _text(r.get("역 이름"))
        if shown and shown != st.name and shown not in st.aliases:
            env.warnings.append(f"{path.name} 역 환경 {i}행: ID {sid}는 {st.name}인데 역 이름 칸은 {shown!r} — ID를 따름")
        vals = {}
        for f in env.fields:
            raw = r.get(f.name)
            t = _text(raw)
            if not t:
                continue
            if f.kind == "숫자":
                try:
                    vals[f.name] = float(t) if "." in t else int(t)
                except ValueError:
                    env.warnings.append(f"{path.name} 역 환경 {i}행 {f.name}: 숫자가 아님 {t!r} — 비움")
                    continue
            elif f.kind == "선택" and t not in f.choices:
                env.warnings.append(f"{path.name} 역 환경 {i}행 {f.name}: {t!r}는 선택지({', '.join(f.choices)})에 없음 — 비움")
                continue
            else:
                vals[f.name] = t
        env.values[sid] = vals
    return env


_env_cache: dict = {}


def environment() -> Environment:
    """역 환경(파일이 바뀌면 다시 읽는다)."""
    path, source = _env_path()
    key = (str(path), path.stat().st_mtime if path.exists() else None)
    if _env_cache.get("key") != key:
        _env_cache.update(key=key, env=read_environment(path, source))
    return _env_cache["env"]
