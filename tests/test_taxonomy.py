"""psd/taxonomy.py 검증: 분류표 읽기와 소분류 이름 맞추기 (2026-11-24 v3). 공개 예시 분류표로 돈다."""
import json

import pytest

from psd import taxonomy as tx


@pytest.fixture
def sample(monkeypatch):
    monkeypatch.setenv("PSD_TAXONOMY", "sample")
    tx.load.cache_clear()
    yield tx.load()
    tx.load.cache_clear()


def test_sample_taxonomy_loads_and_resolves_name_variants(sample):
    assert sample.source == "sample"
    assert sample.resolve("모터&엔코더") == "MOTOR"
    assert sample.resolve("벨트,스크류") == sample.resolve("밸트 · 스크류") == "BELT"  # 띄어쓰기·쉼표·벨트/밸트
    assert sample.resolve("DCU") == sample.resolve("DCU(MCU)") == "DCU"  # 괄호 뺀 이름·별칭
    assert sample.resolve("없는 부위") is None
    assert sample.resolve_group("구동부") == "구동부 장애"


def test_sample_codes_are_a_subset_of_the_private_file_when_present(sample):
    """테스트는 예시 분류표의 코드만 쓴다 — 실제 분류표가 있으면 그 코드가 다 들어 있어야 어느 쪽에서도 돈다."""
    if not tx.PRIVATE_FILE.exists():
        pytest.skip("실제 분류표 없음")
    raw = json.loads(tx.PRIVATE_FILE.read_text(encoding="utf-8"))
    private = {u["code"] for u in raw["units"]} | {u["code"] for u in raw.get("splits") or ()}
    assert {u.code for u in sample.units} <= private


def test_split_unit_follows_its_form_unit_and_is_classified_by_symptom(sample):
    """갈라 낸 단위(가이드레일): 대분류·문 단위는 원래 소분류(가동문)를 따르고, 공식은 없다. 장애내용①로 가른다."""
    g = sample.by_code("GUIDE")
    base = sample.by_code(g.split_from)
    assert (g.split_from, g.group, g.unit, g.formula) == ("SLDOOR", base.group, base.unit, None)
    assert g not in sample.form_units and base in sample.form_units
    assert sample.form_name("GUIDE") == "가동문"
    assert sample.classify("가동문", g.symptoms[0]) == "GUIDE"
    assert sample.classify("가 동 문", " " + g.symptoms[-1]) == "GUIDE"  # 띄어쓰기 무시
    assert sample.classify("가동문", "유리 파손") == sample.classify("가동문", None) == "SLDOOR"
    assert sample.classify("모터&엔코더", g.symptoms[0]) == "MOTOR"  # 다른 소분류는 장애내용과 상관없음


def test_catalog_keeps_only_feedback_targets(sample):
    """예측 대상: 구동부·센서·통신 대분류 전부 + DCU + 가이드레일. 가동문(나머지 도어)·종합제어반·UPS는 빠진다."""
    from psd.catalog import TARGET_CODES, TARGET_GROUPS, is_target

    targets = [u.code for u in sample.units if is_target(u)]
    assert set(targets) == {"MOTOR", "BELT", "HANGER", "OBST", "RF", "DCU", "GUIDE"}
    assert all(sample.by_code(c).group in TARGET_GROUPS or c in TARGET_CODES for c in targets)


@pytest.mark.parametrize("change, msg", [
    (lambda d: d["units"].append(dict(d["units"][0])), "겹칩니다"),
    (lambda d: d["units"][0].update(unit="track"), "door 또는 station"),
    (lambda d: d["units"][0].update(formula="sunlight"), "formula를 모릅니다"),
    (lambda d: d["units"][0].update(group="없는 대분류"), "groups에 없습니다"),
    (lambda d: d["aliases"].update(엉뚱="NOPE"), "aliases"),
    (lambda d: d["splits"][0].update({"from": "NOPE"}), "소분류에 없습니다"),
    (lambda d: d["splits"][0].update(symptoms=[]), "장애내용"),
    (lambda d: d["splits"][0].update(code="MOTOR"), "겹칩니다"),
])
def test_bad_taxonomy_file_is_rejected(tmp_path, monkeypatch, change, msg):
    raw = json.loads(tx.SAMPLE_FILE.read_text(encoding="utf-8"))
    change(raw)
    f = tmp_path / "t.json"
    f.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("PSD_TAXONOMY", str(f))
    tx.load.cache_clear()
    try:
        with pytest.raises(ValueError, match=msg):
            tx.load()
    finally:
        tx.load.cache_clear()
