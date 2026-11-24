"""정비 기록으로 권장 주기를 보정한다 (향후_방향_MVP이후.md 2장, 2026-10-31 시작 / 2026-11-16 v2).

날씨 공식이 낸 주기(psd/cycle.py, DB의 cycle_results.base_years)를 사전값으로 두고, 기록으로 와이블 수명
분포를 베이지안 갱신한다. "물리 공식으로 미리 정한 값을 현장 기록으로 계속 고쳐 나가는" 모델이다 — 기록을
올릴 때마다 다시 계산된다.

v2(2026-11-16, 데이터셋_v2_설계.md 4장)에서 바꾼 것 — 숨은 정답이 있는 더미로 재 보니 v1은 기계 부품을
오히려 정답 반대쪽으로 옮겼다(같은 문서 6장):

1. **형상모수(β)와 공식 치우침(κ)도 데이터로 추정한다.** v1은 2로 고정했다. β는 "오래 쓸수록 고장 위험이 얼마나 빨리 커지나"라
   부품 종류의 성질이다 — 역마다 따로 추정할 만큼 기록이 없으므로 **부품 종류마다 하나**를 전 역 기록으로 함께
   추정하고(β 격자 사후분포), 척도는 역 x 부품마다 둔다. 같은 방식으로 "공식이 이 부품 종류 전체를 몇 배로 잘못
   잡았나"(κ)도 함께 추정해, 역마다 따로 옮겨 가는 것보다 빨리 정답 쪽으로 간다(계층 모델).
2. **수리·일부 교체도 반영한다(가상 나이, Kijima 1형).** v1은 전체 교체만 셌고 고장의 약 40%를 버렸다.
   이제 모든 고장이 데이터가 되고, 조치 뒤 부품의 "나이"를 AGE_FACTOR만큼 남긴다:
   전체 교체 0(새것), 일부 수리 1(고장 직전과 같음 — 신뢰성의 표준 정의), 일부 교체·전체 수리는 0.5(가정).
3. **범위를 낸다.** 사후분포에서 평균 수명을 뽑아 80% 범위(lo~hi)를 준다.

계산 (형상 β를 정하면 고장률 λ = scale^-β 에 감마 사전분포가 켤레라 닫힌 식으로 끝난다):

    구간: 가상 나이 v에서 시작해 x년 쓰고 (고장 | 중도절단)으로 끝남
    S(β) = Σ [(v+x)^β − v^β],  n = 고장 수,  T = Σ_고장 log(v+x)
    사전: λ ~ Gamma(a0, b0),  a0 = PRIOR_FAILURES,  b0 = a0 × (공식 주기 × κ / Γ(1+1/β))^β   (사전 평균 수명 = 공식 × κ)
    사후: λ | β ~ Gamma(a0 + n, b0 + S(β))
    (β, κ)의 주변우도: n log β + (β−1) T + a0 log b0 − log Γ(a0) + log Γ(a0+n) − (a0+n) log(b0+S)
    → 같은 부품 종류의 전 역을 더하고 사전분포(β: 로그정규 중앙값 2, κ: 로그정규 중앙값 1)를 곱해 (β, κ) 격자 위 사후분포
    주기(평균 수명) = exp(사후분포로 평균한 log[Γ(1+1/β) × ((b0+S)/(a0+n))^(1/β)])   (기록이 없으면 정확히 공식 값)

- 아직 고장 안 난 부품도 데이터다(중도절단): "4년째 멀쩡함 = 최소 4년은 버팀"으로 넣는다.
- 예방·함께 정비는 그때까지 고장 안 났다는 중도절단이고, 조치만큼 나이가 줄어든다.
- 기록은 달력 시간으로 센다(날씨로 빨라지는 계절 차이는 1년 평균으로 보면 공식 주기에 이미 들어 있다).
- 데이터가 적어 일반 ML을 역 x 부품마다 따로 학습하는 건 맞지 않는다 — 공식 값에서 출발해 기록이 쌓일수록 옮겨 간다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from psd.cycle import STATIONS
from psd.models import CycleResult, Part, ReplacementPart, ReplacementRecord

PRIOR_FAILURES = 8.0  # v1의 사전값 무게(scripts/check_calibration.py가 v1을 다시 계산할 때만 쓴다). v2는 τ로 정한다. 예전 설명: 날씨 공식 값을 "고장 8건을 본 것"만큼 믿는다. 4로 두면 더미데이터(역 x 부품당 고장 10~20건)의
# 우연한 차이만으로 주기가 ±25%까지 흔들려서, 역 사이 차이가 날씨보다 우연에 더 좌우됐다(2026-10-31 확인)
MAX_YEARS = 10.0  # 제조사 권장 교체주기. 고장 없는 기록이 쌓여 계산이 이보다 길어져도 예정일은 10년을 넘기지 않는다
# (2026-11-01: "제조사 권장보다 늦게 바꾸라는 거냐"는 혼란을 막음 — 날씨 분석의 "10년보다 늦은 교체는 권고하지 않음"과 같은 원칙)

SHAPE_GRID = np.round(np.arange(0.8, 4.0001, 0.05), 2)  # β 후보
SHAPE_PRIOR_MEDIAN, SHAPE_PRIOR_SIGMA = 2.0, 0.35  # β 사전분포(로그정규): 90%가 약 1.1~3.6 — 전자(1 근처)~기계 마모(3 근처)
# 공식 치우침 κ: 그 부품 종류 전 역의 사전 평균 수명 = 공식 주기 × κ. 공식이 부품 종류 전체로 틀렸을 때(예: 실제로는 다 25% 짧음)
# 역마다 따로 옮겨 가면 느리다 — 전 역 기록으로 κ를 함께 추정한다(2026-11-16, 정답 있는 더미에서 확인)
BIAS_GRID = np.exp(np.linspace(-math.log(3.0), math.log(3.0), 71))  # 1/3~3배, log 대칭(기록이 없으면 정확히 공식 값이 되게)
BIAS_PRIOR_SIGMA = 0.3  # κ 사전분포(로그정규, 중앙값 1): 90%가 약 0.6~1.6배
# 2026-11-24 v3: 날씨 공식이 없는 소분류(분류표 formula가 null)는 사전값 10년 자체가 근거 없는 기준이라 κ를 넓게 둔다
# (90%가 약 0.3~3.7배). 기록이 쌓이면 κ가 그 소분류 전체의 실제 수명 쪽으로 빨리 옮겨 간다
BIAS_PRIOR_SIGMA_NO_FORMULA = 0.8
# 역 사이 흩어짐 τ: 같은 부품 종류라도 날씨 공식으로 설명 안 되는 역 차이(log 평균 수명의 표준편차). 이것도 전 역 기록으로
# 추정해 사전값의 무게를 정한다(2026-11-16 5단계). 전에는 "고장 8건만큼"으로 고정이라 β가 작은(우연 고장) 부품에선 너무 가벼워
# 역마다 몇 건 안 되는 고장의 우연한 차이를 그대로 따라갔다(가상 현장 "우연 고장 위주" 시나리오에서 오차가 안 줄던 원인)
TAU_GRID = np.exp(np.linspace(math.log(0.04), math.log(0.6), 12))
TAU_PRIOR_MEDIAN, TAU_PRIOR_SIGMA = 0.15, 0.7
# 조치 뒤 남는 나이의 비율(가상 나이). 0과 1은 신뢰성 용어의 표준 정의(새것과 같음 / 고장 직전과 같음)이다.
# 2026-11-24 v3(양식의 조치사항, 데이터셋_v3_설계.md 3장): 교체(주요 부품) 0, 단순교체(부속 부품) 0.5(근거 없는 가정),
# 수리·조정·리셋·기타는 1(고장 직전과 같음). 현장 판단으로 바꿀 값(향후_방향_MVP이후.md 5절).
# 더미 생성기(scripts/generate_dummy_records.py)의 단순교체 0.4와 일부러 맞추지 않았다: 맞추면 정답을 알려 주는 셈이다
AGE_FACTOR = {"full": 0.0, "partial": 0.5, "repair": 1.0, "adjust": 1.0, "reset": 1.0, "other": 1.0}
RANGE_PROB = 0.8  # lo~hi는 80% 범위
N_DRAWS = 4000  # 범위를 낼 때 사후분포에서 뽑는 수(시드 고정이라 같은 기록이면 같은 범위)

# 역 id → PSD 사용개시일. 부품의 첫 설치일이라 기록 사이 기간을 셀 출발점이 된다(scripts/init_db.py와 같은 값)
PSD_START = {s.order: s.psd_install_date or s.open_date for s in STATIONS}

Interval = tuple[float, float, bool]  # (시작 가상 나이(년), 쓴 기간(년), 고장으로 끝났는지)


@dataclass(frozen=True)
class Calibrated:
    formula_years: float  # 날씨 공식이 낸 주기(사전값)
    years: float  # 기록을 반영한 주기 — 예정일 계산에 쓴다(MAX_YEARS를 넘지 않음)
    failures: int  # 반영한 고장 수(수리로 끝난 고장 포함)
    units: int  # 지켜본 구간 수(고장·정비로 끝난 것 + 지금 쓰는 중인 것)
    observed_years: float  # 지켜본 기간 합(년)
    raw_years: float  # 10년 제한 전 계산값(참고로만 보여 준다)
    lo_years: float = math.nan  # 주기 80% 범위(10년 제한 전)
    hi_years: float = math.nan
    shape: float = 2.0  # 이 부품 종류의 형상모수 β(사후 평균) — 전 역 공통
    shape_lo: float = math.nan  # β 80% 범위
    shape_hi: float = math.nan
    bias: float = 1.0  # 이 부품 종류의 공식 치우침 κ(사후, 전 역 공통) — 1보다 작으면 공식이 길게 잡았다는 뜻
    tau: float = math.nan  # 역 사이 흩어짐 τ(사후, log 표준편차)
    prior_weight: float = PRIOR_FAILURES  # 사전값의 무게(고장 몇 건만큼 공식을 믿었나, 사후 평균)

    @property
    def capped(self) -> bool:
        return self.raw_years > MAX_YEARS

    @property
    def data_share(self) -> float:
        """결과에서 기록이 차지하는 몫(0~1). 고장 수 기준의 대략적인 비율이다."""
        return self.failures / (self.failures + self.prior_weight)


def _as_interval(item: tuple) -> Interval:
    """(쓴 기간, 고장) 두 칸(v1 형식, 시작 나이 0)도 받는다."""
    return (0.0, float(item[0]), bool(item[1])) if len(item) == 2 else (float(item[0]), float(item[1]), bool(item[2]))


def _shape_log_prior() -> np.ndarray:
    z = (np.log(SHAPE_GRID) - math.log(SHAPE_PRIOR_MEDIAN)) / SHAPE_PRIOR_SIGMA
    return -0.5 * z ** 2 - np.log(SHAPE_GRID)


def _tau_log_prior() -> np.ndarray:
    return -0.5 * ((np.log(TAU_GRID) - math.log(TAU_PRIOR_MEDIAN)) / TAU_PRIOR_SIGMA) ** 2


def _bias_log_prior(sigma: float = BIAS_PRIOR_SIGMA) -> np.ndarray:
    return -0.5 * (np.log(BIAS_GRID) / sigma) ** 2  # 격자가 log 간격이라 log 공간 밀도 그대로


def fit_group(formulas: list[float], groups: list[list[tuple]], seed: int = 0, bias_sigma: float = BIAS_PRIOR_SIGMA) -> list[Calibrated]:
    """같은 부품 종류의 여러 역을 함께 보정한다. formulas[g]는 역 g의 공식 주기, groups[g]는 그 역 구간 목록.
    β(형상)·κ(공식 치우침)·τ(역 사이 흩어짐)는 모두 함께 추정하고, 척도·주기·범위는 역마다 낸다.
    bias_sigma: κ 사전분포의 폭(공식이 없는 소분류는 BIAS_PRIOR_SIGMA_NO_FORMULA)."""
    G = len(formulas)
    F = np.asarray(formulas, dtype=float)
    ints = [[_as_interval(i) for i in g if _as_interval(i)[1] > 0] for g in groups]
    gi = np.array([g for g, lst in enumerate(ints) for _ in lst], dtype=int)
    v = np.array([i[0] for lst in ints for i in lst], dtype=float)
    x = np.array([i[1] for lst in ints for i in lst], dtype=float)
    failed = np.array([i[2] for lst in ints for i in lst], dtype=bool)
    end = v + x
    n = np.bincount(gi[failed], minlength=G).astype(float)
    T = np.bincount(gi[failed], weights=np.log(end[failed]), minlength=G)

    from scipy.special import gammaln

    K, J, L = len(SHAPE_GRID), len(BIAS_GRID), len(TAU_GRID)
    B = SHAPE_GRID[:, None, None, None]  # (K, 1, 1, 1)
    gam = np.array([math.gamma(1 + 1 / b) for b in SHAPE_GRID])[:, None, None, None]
    S = np.stack([np.bincount(gi, weights=end ** b - v ** b, minlength=G) for b in SHAPE_GRID]) if len(gi) else np.zeros((K, G))
    S = S[:, None, None, :]  # (K, 1, 1, G)
    # 사전값의 무게 a0: 역 사이 평균 수명의 흩어짐 τ(log 표준편차)에 맞춘다 — Gamma(a0)의 log 분산 ≈ 1/a0이고
    # log 평균 수명 = −log λ / β라서 a0 ≈ 1/(βτ)² + 0.5. β가 작을수록(우연 고장) 같은 τ에 더 무거운 사전값이 된다
    a0 = 1 / (B * TAU_GRID[None, None, :, None]) ** 2 + 0.5  # (K, 1, L, 1)
    b0 = a0 * (F[None, None, None, :] * BIAS_GRID[None, :, None, None] / gam) ** B  # (K, J, L, G): 사전 평균 수명 = 공식 × κ
    a = a0 + n[None, None, None, :]
    b = b0 + S
    log_m = n * np.log(B) + (B - 1) * T + a0 * np.log(b0) - gammaln(a0) + gammaln(a) - a * np.log(b)
    log_post = (log_m.sum(axis=3) + _shape_log_prior()[:, None, None] + _bias_log_prior(bias_sigma)[None, :, None]
                + _tau_log_prior()[None, None, :])  # (K, J, L)
    w = np.exp(log_post - log_post.max())
    w /= w.sum()

    # 점 추정은 사후분포로 평균한 log(평균 수명)의 지수 — 기록이 없으면 κ 사전분포가 log 대칭이라 정확히 F
    point = np.exp((w[..., None] * np.log(gam * (b / a) ** (1 / B))).sum(axis=(0, 1, 2)))
    shape_mean = float((w.sum(axis=(1, 2)) * SHAPE_GRID).sum())
    bias_mean = float(np.exp((w.sum(axis=(0, 2)) * np.log(BIAS_GRID)).sum()))
    tau_mean = float(np.exp((w.sum(axis=(0, 1)) * np.log(TAU_GRID)).sum()))
    prior_weight = float((w * a0[:, :, :, 0]).sum())
    rng = np.random.default_rng(seed)
    flat = rng.choice(K * J * L, size=N_DRAWS, p=w.ravel())
    k, j, ll = flat // (J * L), (flat // L) % J, flat % L
    q = ((1 - RANGE_PROB) / 2, (1 + RANGE_PROB) / 2)
    shape_lo, shape_hi = (float(s) for s in np.quantile(SHAPE_GRID[k], q))
    a_full = np.broadcast_to(a, b.shape)
    out = []
    for g in range(G):
        lam = rng.gamma(a_full[k, j, ll, g], 1 / b[k, j, ll, g])
        life = gam[k, 0, 0, 0] * lam ** (-1 / SHAPE_GRID[k])
        lo, hi = np.quantile(life, q)
        raw = float(point[g])
        out.append(Calibrated(float(F[g]), min(raw, MAX_YEARS), int(n[g]), len(ints[g]), float(sum(i[1] for i in ints[g])), raw,
                              float(lo), float(hi), shape_mean, shape_lo, shape_hi, bias_mean, tau_mean, prior_weight))
    return out


def calibrate(formula_years: float, intervals: list[tuple]) -> Calibrated:
    """역 x 부품 하나만 보정한다(β도 이 기록만으로 추정). 구간은 (시작 나이, 기간, 고장) 또는 (기간, 고장)."""
    return fit_group([formula_years], [intervals])[0]


def part_intervals(start: date, records: list[tuple], today: date, age_factor: dict[str, float] = AGE_FACTOR) -> list[Interval]:
    """부품 하나의 구간 목록. records는 (작업일, 사유 코드[, 조치 코드]). 마지막은 지금 쓰는 중인 구간(중도절단).

    - 사후조치(고장) → 그 구간은 고장으로 끝남. 사전조치(예방) → 그때까지 멀쩡했음(중도절단).
    - 조치 뒤 다음 구간은 가상 나이 = (그때 나이) × age_factor[조치]에서 시작한다. 조치가 없는 기록은 교체로 본다.
    - v2의 점검(이상 없음, 조치 "none")은 v3 양식에 없다. 들어오면 구간을 끊지 않고 건너뛴다.
    """
    out: list[Interval] = []
    age = 0.0
    for replaced_on, reason, *rest in sorted(records):
        action = rest[0] if rest else ("none" if reason == "check" else "full")
        if reason == "check" or action == "none" or replaced_on <= start:
            continue  # 사용개시 전 날짜나 같은 날 두 번 넣은 기록은 뺀다
        x = (replaced_on - start).days / 365.25
        out.append((age, x, reason == "fail"))
        age = (age + x) * age_factor.get(action, 0.0)
        start = replaced_on
    if today > start:
        out.append((age, (today - start).days / 365.25, False))
    return out


_cache: dict = {}  # {"key": 데이터 버전, "map": 전체 역 결과} — 마지막 한 벌만 둔다


def data_version(session: Session, today: date) -> tuple:
    """기록·계산 결과·부품이 바뀌었는지 가늠하는 값. 기록을 넣거나 지우면 개수나 최대 id가 바뀐다."""
    rec = session.execute(select(func.count(ReplacementRecord.id), func.max(ReplacementRecord.id))).one()
    rp = session.execute(select(func.count(ReplacementPart.id), func.max(ReplacementPart.id))).one()
    cr = session.execute(select(func.count(), func.sum(CycleResult.base_years)).select_from(CycleResult)).one()
    parts = session.scalar(select(func.count(Part.id)))
    return (today, tuple(rec), tuple(rp), tuple(cr), parts)


def calibration_map(session: Session, today: date | None = None, station_id: int | None = None,
                    use_cache: bool = True) -> dict[tuple[int, str], Calibrated]:
    """역 x 부품 종류마다 기록을 반영한 주기.

    현황 화면은 API 세 개가 동시에 이걸 부르므로, 기록이 그대로면 마지막 결과를 다시 쓴다.
    업로드 전후 비교처럼 아직 커밋 안 된 상태를 봐야 할 때는 use_cache=False로 부른다.
    β를 전 역 기록으로 함께 추정하므로 한 역만 물어도 전 역을 계산한다.
    """
    today = today or date.today()
    if use_cache:
        key = data_version(session, today)
        if _cache.get("key") != key:
            _cache.update(key=key, map=_compute(session, today))
        full = _cache["map"]
    else:
        full = _compute(session, today)
    return full if station_id is None else {k: v for k, v in full.items() if k[0] == station_id}


def _compute(session: Session, today: date) -> dict[tuple[int, str], Calibrated]:
    formula = {(cr.station_id, cr.part_type_code): cr.base_years for cr in session.execute(select(CycleResult)).scalars()}
    rec_q = (select(ReplacementRecord.station_id, ReplacementRecord.door_id, ReplacementPart.part_type_code,
                    ReplacementRecord.replaced_on, ReplacementRecord.reason, ReplacementRecord.action)
             .join(ReplacementPart, ReplacementPart.record_id == ReplacementRecord.id))
    records: dict[tuple[int, int | None, str], list[tuple[date, str, str]]] = {}
    for sid, door_id, code, replaced_on, reason, action in session.execute(rec_q):
        records.setdefault((sid, door_id, code), []).append((replaced_on, reason, action))

    intervals: dict[tuple[int, str], list[Interval]] = {}
    for sid, door_id, code in session.execute(select(Part.station_id, Part.door_id, Part.part_type_code)):
        intervals.setdefault((sid, code), []).extend(part_intervals(PSD_START[sid], records.get((sid, door_id, code), []), today))

    out: dict[tuple[int, str], Calibrated] = {}
    from psd.catalog import PART_TYPES

    no_formula = {p["code"] for p in PART_TYPES if p["formula"] is None}
    for code in sorted({c for _s, c in formula}):
        keys = sorted(k for k in formula if k[1] == code)
        sigma = BIAS_PRIOR_SIGMA_NO_FORMULA if code in no_formula else BIAS_PRIOR_SIGMA
        fits = fit_group([formula[k] for k in keys], [intervals.get(k, []) for k in keys], bias_sigma=sigma)
        out.update(zip(keys, fits))
    return out
