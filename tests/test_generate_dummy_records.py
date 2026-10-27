"""scripts/generate_dummy_records.py 검증: 와이블 평균 보정 (코드 리뷰로 찾은 버그).

random.weibullvariate(alpha, beta)의 alpha는 평균이 아니라 척도(scale)라, 그대로 쓰면
"공식이 낸 권장 주기를 평균으로 삼는다"는 문서화된 의도와 달리 실제 평균이 더 짧게 나왔다.
"""
import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.generate_dummy_records import WEIBULL_SHAPE, weibull_scale_for_mean  # noqa: E402


def test_weibull_scale_for_mean_is_larger_than_the_target_mean():
    """shape=2일 때는 Γ(1.5)≈0.886 < 1이라, 평균을 맞추려면 scale이 평균보다 커야 한다."""
    scale = weibull_scale_for_mean(10.0, shape=2.0)
    assert scale > 10.0
    assert round(scale, 4) == round(10.0 / 0.8862269254527579, 4)


def test_weibullvariate_with_corrected_scale_has_the_right_empirical_mean():
    random.seed(0)
    target_mean = 7.4
    scale = weibull_scale_for_mean(target_mean, WEIBULL_SHAPE)
    samples = [random.weibullvariate(scale, WEIBULL_SHAPE) for _ in range(20000)]
    # 표본 2만 개면 표준오차가 충분히 작다 — 넉넉하게 2% 오차까지 허용
    assert abs(statistics.mean(samples) - target_mean) / target_mean < 0.02


def test_uncorrected_scale_would_have_undershot_by_about_eleven_percent():
    """회귀 방지: scale 보정을 빼먹으면(예전 버그) 평균이 Γ(1.5)≈0.886배로 짧게 나와야 한다 —
    즉 이 테스트가 "고쳐지기 전엔 평균이 짧았다"는 것 자체를 보여준다."""
    random.seed(0)
    target_mean = 7.4
    samples = [random.weibullvariate(target_mean, WEIBULL_SHAPE) for _ in range(20000)]  # 버그 재현: scale=target_mean
    uncorrected_mean = statistics.mean(samples)
    assert uncorrected_mean < target_mean * 0.95
