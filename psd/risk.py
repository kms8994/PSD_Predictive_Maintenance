"""문·부품마다 "앞으로 6개월 안에 고장 날 확률"을 내는 머신러닝 모델 (2026-11-16, 파이프라인 9단계).

psd/calibrate.py(베이지안 와이블)는 역 x 장애부위(소분류)마다 교체 **주기** 하나를 낸다 — "센텀역 DCU는 약 4년".
이 모델은 부품 **하나하나**의 위험도를 낸다 — "센텀 2-1 DCU는 수리를 세 번 했고 문 전체가 고장이 잦아 위험".
둘은 서로 대신하지 않는다: 주기는 데이터가 적어도 공식에 기대 버티고, 이 모델은 기록이 쌓일수록 크게 좋아진다.

학습 데이터 (부품 x 월 스냅샷):
    매달 1일 t마다, 부품마다 t 전까지의 기록만으로 특징을 만들고(미래 누설 없음),
    정답 = [t, t+6개월) 사이에 그 부품의 고장 기록이 있는지.
    라벨이 다 보이는 달(데이터 마지막 날 6개월 전까지)만 학습에 쓴다.

모델: scikit-learn HistGradientBoostingClassifier(그래디언트 부스팅 트리). 부품 종류는 범주형으로 넣는다.
검증: 시간으로 나눈다 — 최근 스냅샷은 숨겨 두고, 그보다 6개월 이상 앞선 스냅샷으로만 학습해 맞혀 본다
(같은 고장이 학습 라벨과 검증 라벨에 함께 들어가지 않게). 비교 기준은 "수명을 얼마나 썼나"(가상 나이 / 공식 주기) 순서.
재학습(retrain): 기록이 바뀌면 새 모델을 학습해 지금 모델과 같은 새 기간으로 겨루고, 지지 않을 때만 바꾼다(아래).

더미 데이터로 잰 점수는 "시뮬레이터 가정이 맞다면"의 성능이다 — 실제 현장 성능처럼 말하지 않는다.
"""
from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from psd.calibrate import AGE_FACTOR, PSD_START
from psd.catalog import PART_TYPES

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / "models"  # 버전마다 risk_v{id}.joblib (저장소에 안 올림)
HORIZON_DAYS = 182  # 6개월
VAL_MONTHS = 6  # 검증에 숨기는 마지막 스냅샷 달 수
TOP_K = 100  # "위험도 상위 100개 중 실제로 고장 난 비율"
# 소분류 코드(분류표 순서). 모델은 이 순서의 번호를 범주로 받는다 — 분류표가 바뀌면 예전 모델은 못 쓴다(import_risk_model.py가 검사)
PART_CODES = [p["code"] for p in PART_TYPES]

# (이름, 화면 설명). 순서가 모델 입력 순서다
FEATURES = [
    ("part", "장애부위(소분류)"),
    ("formula_years", "날씨 공식 주기(년)"),
    ("age_full", "마지막 전체 교체 뒤(년)"),
    ("life_used", "공식 수명 대비 사용 비율"),
    ("vage_used", "가상 나이 / 공식 주기"),
    ("fails_since_full", "전체 교체 뒤 고장 수"),
    ("fails_total", "전체 고장 수"),
    ("yrs_since_fail", "마지막 고장 뒤(년)"),
    ("door_fail_12m", "같은 문 최근 1년 고장"),
    ("door_fail_36m", "같은 문 최근 3년 고장"),
    ("stn_rate_12m", "이 역 같은 부품 최근 1년 고장률"),
    ("month", "기준 달"),
    # 2026-11-27 날씨 칸(날씨_역분석_엑셀연동_설계.md 3장): 그 역의 평소 날씨 묶음 점수와 지난 90일 평년 대비(psd/weather.py).
    # 앞으로 6개월의 실제 날씨는 예측 때 모르므로 넣지 않는다(넣으면 점수가 부풀려진다). 담당자가 끈 (소분류, 묶음)은 평소 값으로 고정
    ("stn_humid", "이 역 평소 습기"),
    ("stn_heat", "이 역 평소 열"),
    ("stn_cold", "이 역 평소 추위"),
    ("stn_wind", "이 역 평소 바람"),
    ("humid_90d", "지난 90일 습기(평년 대비)"),
    ("heat_90d", "지난 90일 열(평년 대비)"),
    ("cold_90d", "지난 90일 추위(평년 대비)"),
    ("wind_90d", "지난 90일 바람(평년 대비)"),
]
FEATURE_NAMES = [f for f, _ in FEATURES]
WEATHER_BUNDLE_KEYS = ("humid", "heat", "cold", "wind")
WEATHER_WINDOW = 90  # 지난 며칠의 평년 대비


def compatible(model) -> bool:
    """모델이 지금 특징 목록으로 학습됐는지(날씨 칸을 넣기 전 모델은 칸 수가 달라 예측에 못 쓴다)."""
    n = getattr(model, "n_features_in_", None)
    return n is None or n == len(FEATURE_NAMES)


@dataclass
class PartHistory:
    part_id: int
    station_id: int  # 같은 역 기록끼리 묶는 열쇠(가상 노선 여러 벌로 학습할 때는 벌마다 다른 번호, scripts/pretrain_risk_model.py)
    door_id: int | None
    code: str
    events: list[tuple[date, str, str]]  # (발생일, 장애인지, 조치), 날짜순
    start: date | None = None  # PSD 사용개시일. None이면 PSD_START[station_id]

    def __post_init__(self):
        if self.start is None:
            self.start = PSD_START[self.station_id]


def months(start: date, end: date) -> list[date]:
    """start 이후 첫 1일부터 end까지 매달 1일."""
    d = date(start.year, start.month, 1)
    if d < start:
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    out = []
    while d <= end:
        out.append(d)
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return out


class FeatureBuilder:
    """기록에서 특징을 만든다. load()로 DB를 한 번 읽고, rows(t)로 t 시점 특징을 낸다."""

    def __init__(self, parts: list[PartHistory], formula: dict[tuple[int, str], float], aws_of: dict[int, str] | None = None,
                 weather_off: set[tuple[str, str]] | None = None):
        """aws_of: 역 번호 → AWS 지점(없으면 실제 역 목록, 가상 노선 번호는 1000으로 나눈 나머지 역). weather_off: 담당자가 끈 (소분류, 묶음)."""
        self.parts = parts
        self.formula = formula
        self.weather_off = weather_off or set()
        self._init_weather(aws_of)
        self.door_fails: dict[int, list[date]] = {}
        self.stn_fails: dict[tuple[int, str], list[date]] = {}
        self.stn_count: dict[tuple[int, str], int] = {}
        for p in parts:
            self.stn_count[(p.station_id, p.code)] = self.stn_count.get((p.station_id, p.code), 0) + 1
            for d, reason, _a in p.events:
                if reason == "fail":
                    if p.door_id is not None:
                        self.door_fails.setdefault(p.door_id, []).append(d)
                    self.stn_fails.setdefault((p.station_id, p.code), []).append(d)
        for v in (*self.door_fails.values(), *self.stn_fails.values()):
            v.sort()

    def _init_weather(self, aws_of: dict[int, str] | None) -> None:
        """지점 × 날의 묶음 점수 평균(역 평소 날씨)과 평년 대비 누적합(지난 90일 평균을 빠르게)."""
        from psd.cycle import STATIONS

        by_id = {st.id: st.aws_stn for st in STATIONS}
        aws_of = dict(aws_of or {})
        for p in self.parts:
            if p.station_id not in aws_of:
                aws_of[p.station_id] = by_id.get(p.station_id, by_id.get(p.station_id % 1000, ""))
        codes = sorted({a for a in aws_of.values() if a})
        self.aws_idx = {sid: (codes.index(a) if a in codes else -1) for sid, a in aws_of.items()}
        starts = [p.start for p in self.parts] or [date.today()]
        self.w_day0 = min(starts) - timedelta(days=WEATHER_WINDOW + 30)
        n = (max(date.today(), max(starts)) + timedelta(days=400) - self.w_day0).days
        self.stn_mean, self.anom_cum = {}, {}
        try:
            from psd.weather import station_day_matrix

            mat = station_day_matrix(codes, self.w_day0, n, [*WEATHER_BUNDLE_KEYS, *(b + "_anom" for b in WEATHER_BUNDLE_KEYS)]) if codes else {}
        except Exception:  # 날씨 파일이 없으면 평소 값으로
            mat = {}
        for b in WEATHER_BUNDLE_KEYS:
            if mat:
                self.stn_mean[b] = mat[b].mean(axis=1)
                self.anom_cum[b] = np.concatenate([np.zeros((len(codes), 1)), np.cumsum(mat[b + "_anom"], axis=1)], axis=1)

    def weather_row(self, p: PartHistory, t: date) -> list[float]:
        a = self.aws_idx.get(p.station_id, -1)
        i = (t - self.w_day0).days
        stn, recent = [], []
        for b in WEATHER_BUNDLE_KEYS:
            if a < 0 or b not in self.stn_mean or (p.code, b) in self.weather_off or not (WEATHER_WINDOW <= i < self.anom_cum[b].shape[1]):
                stn.append(1.0)
                recent.append(0.0)
                continue
            c = self.anom_cum[b][a]
            stn.append(float(self.stn_mean[b][a]))
            recent.append(float((c[i] - c[i - WEATHER_WINDOW]) / WEATHER_WINDOW))
        return stn + recent

    @staticmethod
    def load(session) -> "FeatureBuilder":
        from sqlalchemy import select

        from psd.models import CycleResult, Part, ReplacementPart, ReplacementRecord

        formula = {(c.station_id, c.part_type_code): c.base_years for c in session.execute(select(CycleResult)).scalars()}
        ev: dict[tuple, list] = {}
        for sid, door, code, day, reason, action in session.execute(
                select(ReplacementRecord.station_id, ReplacementRecord.door_id, ReplacementPart.part_type_code,
                       ReplacementRecord.replaced_on, ReplacementRecord.reason, ReplacementRecord.action)
                .join(ReplacementPart, ReplacementPart.record_id == ReplacementRecord.id)):
            ev.setdefault((sid, door, code), []).append((day, reason, action))
        parts = [PartHistory(pid, sid, door, code, sorted(ev.get((sid, door, code), [])), PSD_START[sid])
                 for pid, sid, door, code in session.execute(select(Part.id, Part.station_id, Part.door_id, Part.part_type_code).order_by(Part.id))]
        from psd.calibrate import weather_overrides

        return FeatureBuilder(parts, formula, weather_off=weather_overrides(session))

    @staticmethod
    def _count(dates: list[date], lo: date, hi: date) -> int:
        return bisect_left(dates, hi) - bisect_left(dates, lo)

    def part_row(self, p: PartHistory, t: date) -> list[float]:
        start = p.start
        formula = self.formula.get((p.station_id, p.code), 10.0)
        last_full, vage, last_ev = start, 0.0, start
        fails_total = fails_since_full = 0
        last_fail = None
        for d, reason, action in p.events:
            if d >= t:
                break
            if d <= last_ev and d != start:
                continue
            vage += (d - last_ev).days / 365.25
            last_ev = d
            if reason == "fail":
                fails_total += 1
                fails_since_full += 1
                last_fail = d
            vage *= AGE_FACTOR.get(action, 0.0)
            if action == "full":
                last_full, fails_since_full = d, 0
        vage += (t - last_ev).days / 365.25
        age_full = (t - last_full).days / 365.25
        door = self.door_fails.get(p.door_id, []) if p.door_id is not None else []
        stn = self.stn_fails.get((p.station_id, p.code), [])
        return [
            PART_CODES.index(p.code),
            formula,
            age_full,
            age_full / formula,
            vage / formula,
            fails_since_full,
            fails_total,
            (t - last_fail).days / 365.25 if last_fail else age_full + 1.0,  # 고장 없음 = 설치 뒤 기간보다 길게
            self._count(door, t - timedelta(days=365), t),
            self._count(door, t - timedelta(days=3 * 365), t),
            self._count(stn, t - timedelta(days=365), t) / self.stn_count[(p.station_id, p.code)],
            t.month,
            *self.weather_row(p, t),
        ]

    def label(self, p: PartHistory, t: date) -> int:
        end = t + timedelta(days=HORIZON_DAYS)
        return int(any(reason == "fail" and t <= d < end for d, reason, _a in p.events))

    def rows(self, t: date, with_label: bool = True) -> tuple[np.ndarray, np.ndarray | None]:
        live = self.live(t)
        X = np.array([self.part_row(p, t) for p in live], dtype=float)
        y = np.array([self.label(p, t) for p in live]) if with_label else None
        return X, y

    def live(self, t: date) -> list[PartHistory]:
        """t에 이미 쓰고 있던 부품(사용개시 뒤)."""
        return [p for p in self.parts if p.start < t]

    def dataset(self, snapshots: list[date]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        Xs, ys, ts = [], [], []
        for t in snapshots:
            X, y = self.rows(t)
            Xs.append(X)
            ys.append(y)
            ts.append(np.full(len(y), t.toordinal()))
        return np.vstack(Xs), np.concatenate(ys), np.concatenate(ts)

    def data_end(self) -> date:
        return max((d for p in self.parts for d, *_ in p.events), default=date.today())


def new_model():
    from sklearn.ensemble import HistGradientBoostingClassifier

    # 작은 트리(잎 7개, 잎마다 300행 이상): 고장이 대부분 우연이라 큰 트리는 잡음을 외웠다. 2026-11-16 검증에서
    # 잎 15개·80행(AUC 0.606) < 이 설정(0.627) ≈ 깊이 3(0.628). 같은 검증으로 고른 것이라 점수가 조금 낙관적일 수 있다
    return HistGradientBoostingClassifier(
        learning_rate=0.03, max_iter=200, max_leaf_nodes=7, min_samples_leaf=300, l2_regularization=1.0,
        categorical_features=[FEATURE_NAMES.index("part")], random_state=0,
    )


def _scores(y: np.ndarray, p: np.ndarray, t: np.ndarray) -> dict:
    """AUC, 평균 정밀도, 달마다 위험도 상위 TOP_K개 중 실제 고장 비율(평균)과 기준 고장률 대비 배수."""
    from sklearn.metrics import average_precision_score, roc_auc_score

    tops = []
    for tt in np.unique(t):
        m = t == tt
        idx = np.argsort(-p[m])[:TOP_K]
        tops.append(y[m][idx].mean())
    base = float(y.mean())
    top = float(np.mean(tops))
    return {"auc": float(roc_auc_score(y, p)), "ap": float(average_precision_score(y, p)),
            "top_k": TOP_K, "top_k_precision": top, "base_rate": base, "lift": top / base if base else math.nan}


def label_complete_snapshots(data_end: date) -> list[date]:
    """정답(6개월 안 고장)이 다 보이는 스냅샷 달: 사용개시 반년 뒤 ~ 데이터 마지막 날 6개월 전."""
    return months(min(PSD_START.values()) + timedelta(days=180), data_end - timedelta(days=HORIZON_DAYS))


def fit(fb: FeatureBuilder, snaps: list[date]):
    X, y, _ = fb.dataset(snaps)
    return new_model().fit(X, y), len(y)


def score_on(model, fb: FeatureBuilder, snaps: list[date]) -> dict:
    X, y, t = fb.dataset(snaps)
    return _scores(y, model.predict_proba(X)[:, 1], t)


def baseline_on(fb: FeatureBuilder, snaps: list[date]) -> dict:
    X, y, t = fb.dataset(snaps)
    return _scores(y, X[:, FEATURE_NAMES.index("vage_used")], t)


# ---------- 재학습 흐름 (파이프라인 10단계) ----------
# 새 모델(도전자)과 지금 쓰는 모델(챔피언)을 둘 다 학습 때 못 본 최근 기간(검증 기간)으로 맞혀 보고,
# 도전자가 지지 않을 때만 바꿔 쓴다. 검증 기간 = 챔피언이 학습한 마지막 스냅샷보다 뒤이면서 정답이 다 보이는 달
# (최대 VAL_MONTHS개). 도전자는 그 기간보다 6개월 이상 앞선 스냅샷으로만 학습한 판으로 잰다(같은 고장이 학습·검증
# 라벨에 함께 들어가지 않게). 통과하면 전체 스냅샷으로 다시 학습한 판을 쓴다.
GATE_TOLERANCE = 0.005  # 도전자 AUC가 챔피언보다 이만큼 낮아도 지지 않은 것으로 본다(우연한 흔들림)
MIN_AUC = 0.55


def decide(challenger: dict, baseline: dict, champion: dict | None, has_champion: bool) -> tuple[bool, str]:
    """(바꿀지, 이유)."""
    c = challenger["auc"]
    if c < MIN_AUC:
        return False, f"새 모델 AUC {c:.3f}가 최소 기준 {MIN_AUC}보다 낮음"
    if c <= baseline["auc"]:
        return False, f"새 모델 AUC {c:.3f}가 기준 방법(수명 사용 비율 순) {baseline['auc']:.3f}보다 못함"
    if not has_champion:
        return True, f"비교할 지금 모델 없음 - 검증 AUC {c:.3f}, 기준 방법 {baseline['auc']:.3f}보다 나아 적용"
    if champion is None:
        return False, "지금 모델이 못 본 새 기간이 아직 없어 비교할 수 없음 - 지금 모델 유지"
    if c >= champion["auc"] - GATE_TOLERANCE:
        return True, f"새 모델 AUC {c:.3f} ≥ 지금 모델 {champion['auc']:.3f}(같은 새 기간)"
    return False, f"새 모델 AUC {c:.3f} < 지금 모델 {champion['auc']:.3f}(같은 새 기간) - 지금 모델 유지"


def run_retrain(fb: FeatureBuilder, champion: dict | None) -> dict:
    """DB 없이 도는 핵심. champion = {"model", "last_train_snapshot"} 또는 None.
    돌려주는 dict: accept, reason, model(전체로 다시 학습한 판), 점수들."""
    from sklearn.inspection import permutation_importance

    data_end = fb.data_end()
    snaps = label_complete_snapshots(data_end)
    fresh = [t for t in snaps if champion and t > champion["last_train_snapshot"]]
    holdout = (fresh or snaps)[-VAL_MONTHS:]
    train = [t for t in snaps if t + timedelta(days=HORIZON_DAYS) <= holdout[0]]
    val_model, _ = fit(fb, train)
    ch = score_on(val_model, fb, holdout)
    base = baseline_on(fb, holdout)
    champ = score_on(champion["model"], fb, holdout) if champion and fresh else None
    accept, reason = decide(ch, base, champ, champion is not None)

    Xh, yh, _ = fb.dataset(holdout)
    imp = permutation_importance(val_model, Xh, yh, scoring="roc_auc", n_repeats=3, random_state=0)
    importance = sorted(((FEATURES[i][1], float(imp.importances_mean[i])) for i in range(len(FEATURES))), key=lambda x: -x[1])
    final, rows = fit(fb, snaps)
    return {"accept": accept, "reason": reason, "model": final, "data_end": data_end, "last_train_snapshot": snaps[-1],
            "train_rows": rows, "importance": importance,
            "metrics": {"holdout": [holdout[0].isoformat(), holdout[-1].isoformat()], "holdout_is_new": bool(fresh),
                        "challenger": ch, "champion": champ, "baseline": base}}


def data_key(session) -> str:
    """학습에 쓰는 기록·계산 결과 상태. 같으면 다시 학습하지 않는다(오늘 날짜는 넣지 않음)."""
    from psd.calibrate import data_version

    return repr(data_version(session, date(2000, 1, 1))[1:])


def _model_file(model_id: int) -> Path:
    return MODEL_DIR / f"risk_v{model_id}.joblib"


def active_row(session):
    from sqlalchemy import select

    from psd.models import RiskModel

    return session.execute(select(RiskModel).where(RiskModel.status == "active").order_by(RiskModel.id.desc())).scalars().first()


_loaded: dict = {}


def load_model(model_id: int):
    """버전 id의 모델 파일(한 번 읽으면 기억). 파일이 없으면 None."""
    if model_id not in _loaded:
        path = _model_file(model_id)
        if not path.exists():
            return None
        import joblib

        _loaded[model_id] = joblib.load(path)
    return _loaded[model_id]


def retrain(session, trigger: str, force: bool = False) -> dict:
    """새 모델을 학습해 지금 모델과 비교하고, 결과를 risk_models에 1줄 남긴다(커밋까지 한다).
    기록이 지난 학습 때와 같으면(force가 아니면) 학습하지 않고 {"skipped": True}."""
    from sqlalchemy import select

    from psd.models import RiskModel

    key = data_key(session)
    last = session.execute(select(RiskModel).order_by(RiskModel.id.desc())).scalars().first()
    if last is not None and last.data_key == key and not force:
        return {"skipped": True, "reason": "지난 학습 뒤 기록이 바뀌지 않음", "version": last.id}

    champ_row = active_row(session)
    fb = FeatureBuilder.load(session)
    champion, note = None, ""
    if champ_row is not None:
        m = load_model(champ_row.id)
        if m is not None and not compatible(m):
            # 특징이 바뀐 뒤(2026-11-27 날씨 칸 추가) 처음 학습 — 칸 수가 달라 지금 모델로 맞혀 볼 수 없다. 겨루지 않고 새로 세운다
            note = f"v{champ_row.id}는 날씨 칸을 넣기 전 모델이라 겨루지 않고 새로 세움. "
        elif m is not None and champ_row.data_end <= fb.data_end():
            champion = {"model": m, "last_train_snapshot": champ_row.last_train_snapshot}
        elif m is not None:
            # 업로드를 되돌려 지금 모델이 이제 없는 기록까지 보고 학습한 상태 — 겨룰 상대로 쓰지 않고 새로 세운다
            note = f"v{champ_row.id}가 되돌린 기록(~{champ_row.data_end})까지 보고 학습해 새로 세움. "
    r = run_retrain(fb, champion)
    r["reason"] = note + r["reason"]
    if note and r["accept"] is False and "최소 기준" not in r["reason"] and "기준 방법" not in r["reason"]:
        r["accept"] = True

    row = RiskModel(trained_at=datetime.now().replace(microsecond=0), trigger=trigger, data_key=key, data_end=r["data_end"],
                    last_train_snapshot=r["last_train_snapshot"], train_rows=r["train_rows"],
                    status="active" if r["accept"] else "rejected", reason=r["reason"][:300], metrics=r["metrics"],
                    importance=r["importance"], file_name="")
    session.add(row)
    session.flush()
    import joblib

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(r["model"], _model_file(row.id))
    row.file_name = _model_file(row.id).name
    if r["accept"] and champ_row is not None:
        champ_row.status = "retired"
    session.commit()
    _loaded[row.id] = r["model"]
    return {"skipped": False, "version": row.id, "accepted": r["accept"], "reason": r["reason"], "metrics": r["metrics"]}


_pred_cache: dict = {}


def predict_all(session, today: date | None = None) -> dict[int, dict] | None:
    """부품 id → {"prob", "rank", "of", "features"(화면용 값)}. 쓰는 모델이 없으면 None.
    기록·모델 버전이 그대로면 다시 계산하지 않는다."""
    from psd.calibrate import data_version

    row = active_row(session)
    model = load_model(row.id) if row is not None else None
    if model is None or not compatible(model):  # 날씨 칸을 넣기 전 모델이면 다시 학습할 때까지 위험도를 내지 않는다
        return None
    today = today or date.today()
    key = (data_version(session, today), row.id)
    if _pred_cache.get("key") != key:
        fb = FeatureBuilder.load(session)
        X, _ = fb.rows(today, with_label=False)
        live = fb.live(today)
        prob = model.predict_proba(X)[:, 1]
        order = np.argsort(-prob)
        rank = np.empty(len(prob), dtype=int)
        rank[order] = np.arange(1, len(prob) + 1)
        _pred_cache.update(key=key, map={
            p.part_id: {"prob": float(prob[i]), "rank": int(rank[i]), "of": len(prob),
                        "features": dict(zip(FEATURE_NAMES, X[i].tolist()))}
            for i, p in enumerate(live)
        })
    return _pred_cache["map"]
