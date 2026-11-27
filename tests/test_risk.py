"""psd/risk.py 검증: 특징에 미래가 새지 않는지, 라벨·스냅샷·점수 계산. DB 없이 돈다."""
from datetime import date

import numpy as np
import pytest

from psd.risk import FEATURE_NAMES, FeatureBuilder, PartHistory, _scores, months

START = date(2022, 1, 1)  # PSD_START 역 1(부전)


def _fb(events_a, events_b=()):
    parts = [PartHistory(1, 1, 10, "DCU", sorted(events_a)), PartHistory(2, 1, 10, "MOTOR", sorted(events_b))]
    return FeatureBuilder(parts, {(1, "DCU"): 7.0, (1, "MOTOR"): 6.0})


def feat(fb, pid, t):
    p = next(p for p in fb.parts if p.part_id == pid)
    return dict(zip(FEATURE_NAMES, fb.part_row(p, t)))


def test_features_use_only_the_past():
    fb = _fb([(date(2023, 3, 1), "fail", "repair"), (date(2024, 6, 1), "fail", "full")])
    before = feat(fb, 1, date(2023, 3, 1))  # 그날 고장은 아직 모름
    assert before["fails_total"] == 0 and before["door_fail_36m"] == 0
    mid = feat(fb, 1, date(2024, 1, 1))
    assert mid["fails_total"] == 1 and mid["fails_since_full"] == 1
    assert mid["age_full"] == pytest.approx(2.0, abs=0.01)  # 수리는 설치일을 안 바꾼다
    after = feat(fb, 1, date(2025, 6, 1))
    assert after["fails_since_full"] == 0 and after["age_full"] == pytest.approx(1.0, abs=0.01)


def test_door_history_counts_other_parts_on_the_same_door():
    fb = _fb([], [(date(2024, 5, 1), "fail", "full"), (date(2025, 5, 1), "fail", "full")])
    f = feat(fb, 1, date(2025, 6, 1))  # DCU 자신은 고장 없음
    assert f["fails_total"] == 0 and f["door_fail_12m"] == 1 and f["door_fail_36m"] == 2


def test_label_is_failure_in_the_next_six_months():
    fb = _fb([(date(2024, 5, 1), "fail", "full"), (date(2024, 9, 1), "prev", "full")])
    p = fb.parts[0]
    assert fb.label(p, date(2024, 1, 1)) == 1
    assert fb.label(p, date(2024, 5, 2)) == 0  # 그 뒤 6개월엔 사전조치만
    assert fb.label(p, date(2023, 1, 1)) == 0


def test_months_are_first_days():
    assert months(date(2022, 1, 15), date(2022, 4, 1)) == [date(2022, 2, 1), date(2022, 3, 1), date(2022, 4, 1)]


def test_scores_reward_ranking_failures_first():
    y = np.array([1, 0, 0, 0] * 50)
    t = np.zeros(len(y))
    good = _scores(y, y + 0.01 * np.arange(len(y)) % 1, t)
    rand = _scores(y, np.random.default_rng(0).random(len(y)), t)
    assert good["auc"] > 0.95 > rand["auc"]
    assert good["base_rate"] == pytest.approx(0.25)


# ---------- 재학습 판정 (2026-11-16 파이프라인 10단계) ----------
from psd.risk import GATE_TOLERANCE, decide  # noqa: E402


def S(auc):
    return {"auc": auc}


def test_first_model_needs_to_beat_baseline():
    assert decide(S(0.62), S(0.57), None, has_champion=False)[0]
    assert not decide(S(0.56), S(0.57), None, has_champion=False)[0]
    assert not decide(S(0.54), S(0.50), None, has_champion=False)[0]  # 최소 기준


def test_challenger_replaces_champion_only_if_not_worse_on_new_period():
    assert decide(S(0.63), S(0.57), S(0.62), True)[0]
    assert decide(S(0.62 - GATE_TOLERANCE / 2), S(0.57), S(0.62), True)[0]  # 흔들림 안
    ok, why = decide(S(0.60), S(0.57), S(0.62), True)
    assert not ok and "유지" in why


def test_no_new_period_keeps_champion():
    ok, why = decide(S(0.70), S(0.57), None, has_champion=True)
    assert not ok and "새 기간" in why


def test_weather_features_follow_station_and_respect_manual_off():
    """날씨 칸(2026-11-27): 역마다 그 AWS 지점의 평소 묶음 점수, 담당자가 끈 (소분류, 묶음)은 평소 값(1, 0)."""
    from datetime import date

    from psd.risk import FEATURE_NAMES, FeatureBuilder, PartHistory, compatible

    parts = [PartHistory(1, 1, 10, "MOTOR", [], date(2022, 1, 1)), PartHistory(2, 1, 11, "DCU", [], date(2022, 1, 1))]
    fb = FeatureBuilder(parts, {}, aws_of={1: "937"}, weather_off={("DCU", "humid")})
    t = date(2024, 9, 1)
    a = dict(zip(FEATURE_NAMES, fb.part_row(parts[0], t)))
    b = dict(zip(FEATURE_NAMES, fb.part_row(parts[1], t)))
    assert len(fb.part_row(parts[0], t)) == len(FEATURE_NAMES)
    assert a["stn_humid"] > 1.0  # 해운대는 습한 지점
    assert b["stn_humid"] == 1.0 and b["humid_90d"] == 0.0  # 끈 칸은 평소 값
    assert b["stn_heat"] == a["stn_heat"]  # 다른 묶음은 그대로

    class Old:
        n_features_in_ = 12

    assert not compatible(Old())
