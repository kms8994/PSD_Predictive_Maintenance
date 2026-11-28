"""장애부위 분류표 읽기 (2026-11-24, 데이터셋_v3_설계.md 6장).

코레일 장애 이력 양식의 분류(대분류·소분류·장애내용·부품)는 코레일 자료라 공개 저장소에 올리지 않는다.
실제 분류표는 data/private/장애분류.json(.gitignore)에 두고, 없으면 공개 예시 data/sample/장애분류_예시.json을 읽는다.
예시의 소분류 코드는 실제 분류표의 코드 중 일부와 같아서, 테스트는 그 코드만 쓰면 어느 쪽에서도 돈다.

환경변수 PSD_TAXONOMY로 파일을 고를 수 있다: "sample" 또는 JSON 파일 경로.

파일 형식:
    groups: 대분류 이름 목록("구동부 장애" ...)
    units: [{code, name, group, unit("door"|"station"), formula("motor"|"dcm"|"humid"|"fixed"|null),
             symptoms(장애내용① 목록, 비면 자유 입력), main_parts, minor_parts}, ...]
    aliases: {현장에서 다르게 적는 소분류 표기: 코드}
    splits: [{code, name, from(양식 소분류 코드), symptoms(이 장애내용①이면 갈라 냄), main_parts, minor_parts}, ...] — 없어도 된다

갈라 낸 단위(splits, 2026-11-28): 양식에는 없는데 현장이 따로 보고 싶어 하는 부위. 예: 가이드레일은 양식의
'가동문' 소분류 안에서 장애내용①이 가이드레일 쪽 장애(분류표 splits에 적은 목록)인 기록이다. 엑셀에는 '가동문'으로 적히고,
읽을 때 장애내용①을 보고 갈라 낸다(classify). 내려받을 때는 다시 '가동문'으로 쓴다(form_name).
대분류·문/역 단위는 원래 소분류를 따르고, 날씨 공식은 없다(데이터로 배운다).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_FILE = ROOT / "data" / "private" / "장애분류.json"
SAMPLE_FILE = ROOT / "data" / "sample" / "장애분류_예시.json"
FORMULAS = {"motor", "dcm", "humid", "fixed", None}  # psd/cycle.py part_cycle이 아는 공식


@dataclass(frozen=True)
class Unit:
    code: str
    name: str
    group: str
    unit: str  # "door" | "station"
    formula: str | None
    symptoms: tuple[str, ...]
    main_parts: tuple[str, ...]
    minor_parts: tuple[str, ...]
    split_from: str | None = None  # 갈라 낸 단위면 양식의 원래 소분류 코드


@dataclass(frozen=True)
class Taxonomy:
    source: str  # "private" | "sample" | 파일 경로
    groups: tuple[str, ...]
    units: tuple[Unit, ...]  # 양식 소분류 + 갈라 낸 단위(뒤쪽)
    aliases: tuple[tuple[str, str], ...]  # (현장 표기, 코드)

    def by_code(self, code: str) -> Unit:
        return next(u for u in self.units if u.code == code)

    @property
    def form_units(self) -> tuple[Unit, ...]:
        """양식에 있는 소분류만(갈라 낸 단위 빼고). 내려받기 양식의 드롭다운 목록."""
        return tuple(u for u in self.units if u.split_from is None)

    def form_name(self, code: str) -> str:
        """엑셀 '장애부위②(소분류)'에 쓸 이름. 갈라 낸 단위는 원래 소분류 이름."""
        u = self.by_code(code)
        return self.by_code(u.split_from).name if u.split_from else u.name

    def classify(self, unit_text: str, symptom: str | None = None) -> str | None:
        """엑셀 한 줄의 소분류 글 + 장애내용① → 코드. 소분류가 갈라 낸 단위의 원래 소분류이고
        장애내용①이 그 단위의 목록에 있으면 갈라 낸 단위 코드(예: 가동문 + "이탈" → GUIDE)."""
        code = self.resolve(unit_text)
        if code is None or not symptom:
            return code
        key = normalize(symptom)
        for u in self.units:
            if u.split_from == code and key in {normalize(s) for s in u.symptoms}:
                return u.code
        return code

    def resolve(self, text: str) -> str | None:
        """현장이 적은 소분류 이름 → 코드. 띄어쓰기·쉼표·가운뎃점·괄호 차이는 무시한다. 못 찾으면 None."""
        return _lookup(self).get(normalize(text))

    def resolve_group(self, text: str) -> str | None:
        """대분류 이름 → 목록의 이름. "구동부"처럼 '장애'를 빼고 적어도 받는다."""
        key = normalize(text).removesuffix("장애")
        return next((g for g in self.groups if normalize(g).removesuffix("장애") == key), None)


def normalize(text: str) -> str:
    """비교용 표기: 공백·쉼표·가운뎃점·빗금을 빼고, '벨트'는 양식 목록 표기('밸트')로 맞춘다."""
    t = re.sub(r"[\s,·/]", "", str(text or ""))
    return t.replace("벨트", "밸트")


@lru_cache(maxsize=None)
def _lookup(tax: Taxonomy) -> dict[str, str]:
    out: dict[str, str] = {}
    for u in tax.units:
        out[normalize(u.name)] = u.code
        out[normalize(re.sub(r"\(.*?\)", "", u.name))] = u.code  # "RF장치(지상)" → "RF장치"도
        out[normalize(u.code)] = u.code
    for alias, code in tax.aliases:
        out[normalize(alias)] = code
    return out


def _path() -> tuple[Path, str]:
    env = os.environ.get("PSD_TAXONOMY", "").strip()
    if env == "sample":
        return SAMPLE_FILE, "sample"
    if env:
        return Path(env), env
    return (PRIVATE_FILE, "private") if PRIVATE_FILE.exists() else (SAMPLE_FILE, "sample")


@lru_cache(maxsize=None)
def load() -> Taxonomy:
    path, source = _path()
    raw = json.loads(path.read_text(encoding="utf-8"))
    units = tuple(
        Unit(u["code"], u["name"], u["group"], u["unit"], u.get("formula"), tuple(u.get("symptoms") or ()),
             tuple(u.get("main_parts") or ()), tuple(u.get("minor_parts") or ()))
        for u in raw["units"]
    )
    form_codes = {u.code for u in units}
    splits = []
    for sp in raw.get("splits") or ():
        if sp.get("from") not in form_codes:
            raise ValueError(f"분류표 {path}: 갈라 낸 단위 {sp.get('name')!r}의 from {sp.get('from')!r}이 소분류에 없습니다")
        if not sp.get("symptoms"):
            raise ValueError(f"분류표 {path}: 갈라 낸 단위 {sp.get('name')!r}에 장애내용(symptoms)이 없습니다")
        base = next(u for u in units if u.code == sp["from"])
        splits.append(Unit(sp["code"], sp["name"], base.group, base.unit, None, tuple(sp["symptoms"]),
                           tuple(sp.get("main_parts") or ()), tuple(sp.get("minor_parts") or ()), split_from=base.code))
    units = units + tuple(splits)
    codes = [u.code for u in units]
    if len(set(codes)) != len(codes):
        raise ValueError(f"분류표 {path}: 소분류 코드가 겹칩니다")
    for u in units:
        if u.unit not in ("door", "station"):
            raise ValueError(f"분류표 {path}: {u.name}의 unit은 door 또는 station이어야 합니다: {u.unit!r}")
        if u.formula not in FORMULAS:
            raise ValueError(f"분류표 {path}: {u.name}의 formula를 모릅니다: {u.formula!r}")
        if u.group not in raw["groups"]:
            raise ValueError(f"분류표 {path}: {u.name}의 대분류 {u.group!r}가 groups에 없습니다")
    unknown = {c for c in raw.get("aliases", {}).values() if c not in codes}
    if unknown:
        raise ValueError(f"분류표 {path}: aliases가 없는 코드를 가리킵니다: {sorted(unknown)}")
    return Taxonomy(source, tuple(raw["groups"]), units, tuple(raw.get("aliases", {}).items()))
