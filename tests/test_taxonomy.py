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
    private = {u["code"] for u in json.loads(tx.PRIVATE_FILE.read_text(encoding="utf-8"))["units"]}
    assert {u.code for u in sample.units} <= private


@pytest.mark.parametrize("change, msg", [
    (lambda d: d["units"].append(dict(d["units"][0])), "겹칩니다"),
    (lambda d: d["units"][0].update(unit="track"), "door 또는 station"),
    (lambda d: d["units"][0].update(formula="sunlight"), "formula를 모릅니다"),
    (lambda d: d["units"][0].update(group="없는 대분류"), "groups에 없습니다"),
    (lambda d: d["aliases"].update(엉뚱="NOPE"), "aliases"),
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
