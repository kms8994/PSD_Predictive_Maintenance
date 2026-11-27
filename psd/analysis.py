"""역별·시기별 고장 분석 (2026-11-27, 날씨_역분석_엑셀연동_설계.md 1·2장).

질문 1: 어느 역이 가장 고장이 잦은가 — 지리·환경(날씨) 특성 때문인가, 그 역의 어느 장애부위·어느 문이 잦은가.
질문 2: 역마다 언제(계절·달·요일·공휴일·날씨 조건) 고장이 잦은가.

**건수로 바로 줄 세우지 않는다.** 문이 많은 역, 부품이 늙은 역, 고장이 잦은 소분류가 많은 역이 당연히 많다. 그래서
"기대 고장"을 먼저 만든다:
- 노선 전체에서 소분류 × 나이 구간(마지막 전체 교체 뒤 0~1, 1~2, …, 5년~)마다 하루 고장률을 잰다.
- 부품마다 그날의 소분류·나이 구간 고장률을 더한 것 = 그 역·그 소분류·그 날의 기대 고장(평범한 역이라면 이만큼).
- **고장 비율 = 실제 ÷ 기대.** 1.3이면 평범한 역보다 1.3배. 실제는 사후조치(고장) 기록만 센다.

역 순위는 건수가 적은 역이 우연히 1위가 되지 않게 노선 평균 쪽으로 당긴다(경험적 베이즈, 감마 사전분포 — 역 사이 흩어짐을
데이터로 잰다). 80% 범위를 같이 낸다.

**언제**는 같은 역·같은 달 안에서 비교한다(층화 Mantel-Haenszel 비율비): "같은 역의 같은 달 안에서, 비 온 날(과 그 뒤 일주일)이
다른 날보다 몇 배" — 계절 자체가 끼어들지 않는다. 평년보다 습했던 달처럼 달 단위 날씨는 같은 역·같은 달(다른 해)끼리 비교한다.
계절 효과는 세 층으로 본다: ① 달별 고장 비율(원인 묻지 않음) ② 같은 계절 안의 날씨 차이로 잰 날씨 영향 ③ 날씨 아닌 것
(요일·공휴일, "이 역만 여름에 유독" 같은 역 × 계절 차이).

알아둘 점:
- 역 날씨는 AWS 지점 7곳뿐이라(역 24개가 7개 날씨를 나눠 씀) 역 사이 날씨 차이로 잰 영향은 범위가 넓다. 날씨 영향은 같은 역의
  시기별 차이에서 더 잘 보인다(설계 0장).
- 역 구조(지하·고가 등) 값이 역환경 파일에 들어오면 역 순위 설명에 함께 쓴다(지금은 비어 있음).
- 모두 더미(숨은 정답이 있는 가상 기록) 기준 결과다.
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
HOLIDAY_FILE = ROOT / "data" / "calendar" / "공휴일.csv"
AGE_BANDS = (1, 2, 3, 4, 5)  # 마지막 전체 교체 뒤 년 — 0~1, 1~2, 2~3, 3~4, 4~5, 5~
Z80 = 1.2816  # 80% 범위
LAG_DAYS = 7  # "그 날과 그 뒤 일주일"
SEASONS = {"봄": (3, 4, 5), "여름": (6, 7, 8), "가을": (9, 10, 11), "겨울": (12, 1, 2)}
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
# 날씨 조건(그 역 AWS 지점의 하루 값). 기준은 scripts/preprocess_aws_weather.py의 하루 값
CONDITIONS = {
    "rain": ("비 온 날", lambda w: w["rn_hours"] >= 1),
    "heavy_rain": ("폭우(시간당 10mm↑)", lambda w: w["rn_max"] >= 10),
    "cond": ("결로 많은 날(6시간↑)", lambda w: w["cond_hours"] >= 6),
    "heat": ("폭염(30℃↑ 4시간↑)", lambda w: w["hot_hours"] >= 4),
    "cold": ("한파(영하 6시간↑)", lambda w: w["frz_hours"] >= 6),
    "thaw": ("얼었다 녹은 날", lambda w: w["thaw_count"] >= 1),
    "wind": ("강풍·비바람", lambda w: (w["wind_hours"] >= 3) | (w["rain_wind_hours"] >= 2)),
}


def load_holidays(path: Path = HOLIDAY_FILE) -> dict[date, str]:
    """공휴일 고정 파일(data/calendar/공휴일.csv, 2022~2026). 폐쇄망에서도 돌게 파일로 둔다 — 해마다 다음 해 줄을 더한다.
    음력 공휴일·대체공휴일·임시공휴일은 2026-11-27에 기억으로 적었다(관보로 확인 필요)."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return {date.fromisoformat(r["date"]): r["name"] for r in csv.DictReader(f)}


# ---------- 통계 도구 ----------


def gamma_ci(obs: float, exp: float, k: float = 0.0) -> tuple[float, float, float]:
    """고장 비율(실제 ÷ 기대)의 감마 사후분포: 평균과 80% 범위. k = 노선 평균(1) 쪽으로 당기는 세기(기대 고장 k건만큼)."""
    from scipy.stats import gamma

    a, b = obs + k, exp + k
    if b <= 0:
        return (math.nan, math.nan, math.nan)
    if a <= 0:
        return (0.0, 0.0, float(gamma.ppf(0.9, 0.5, scale=1 / b)))
    return (a / b, float(gamma.ppf(0.1, a, scale=1 / b)), float(gamma.ppf(0.9, a, scale=1 / b)))


def shrink_strength(obs: np.ndarray, exp: np.ndarray) -> float:
    """역 사이 진짜 흩어짐(분산)을 적률법으로 재서 당기는 세기 k를 정한다: 흩어짐이 작을수록 크게 당긴다."""
    ok = exp > 0
    if ok.sum() < 3:
        return 50.0
    r = obs[ok] / exp[ok]
    w = exp[ok] / exp[ok].sum()
    mean = float((w * r).sum())
    var_total = float((w * (r - mean) ** 2).sum())
    var_noise = float((w * mean / exp[ok]).sum())  # 포아송 잡음
    between = var_total - var_noise
    if between <= 1e-4:
        return 1000.0
    return float(np.clip(1 / between, 2.0, 1000.0))


def mh_rate_ratio(O: np.ndarray, E: np.ndarray, X: np.ndarray, strata: np.ndarray) -> dict:
    """층화 Mantel-Haenszel 비율비: 조건 날(X)의 고장 비율 ÷ 같은 층(역 × 달)의 다른 날 고장 비율.
    E(기대 고장)를 사람-시간 대신 쓴다 — 나이·소분류 구성은 이미 E에 들어 있다. 80% 범위는 Greenland-Robins 분산."""
    O, E, X, strata = (np.asarray(a).ravel() for a in (O, E, X, strata))
    ok = E > 0
    O, E, X, strata = O[ok], E[ok], X[ok], strata[ok]
    _, k = np.unique(strata, return_inverse=True)
    n = k.max() + 1 if len(k) else 0
    a = np.bincount(k, O * X, n)
    b = np.bincount(k, O * ~X, n)
    n1 = np.bincount(k, E * X, n)
    n0 = np.bincount(k, E * ~X, n)
    t = n1 + n0
    use = (n1 > 0) & (n0 > 0) & ((a + b) > 0)
    a, b, n1, n0, t = a[use], b[use], n1[use], n0[use], t[use]
    R, S = float((a * n0 / t).sum()), float((b * n1 / t).sum())
    out = {"obs": int(a.sum()), "exp_share": float(n1.sum() / max(t.sum(), 1e-9)), "strata": int(use.sum())}
    if R <= 0 or S <= 0:
        return {**out, "rr": math.nan, "lo": math.nan, "hi": math.nan}
    rr = R / S
    var = float((n1 * n0 * (a + b) / t ** 2).sum()) / (R * S)
    se = math.sqrt(var)
    return {**out, "rr": rr, "lo": rr * math.exp(-Z80 * se), "hi": rr * math.exp(Z80 * se)}


def poisson_rr(obs: np.ndarray, exp: np.ndarray, x: np.ndarray) -> dict:
    """역 단위 포아송 회귀(기대 고장을 offset으로): 고장 비율 = exp(b0 + b1·x). x는 표준화한 값(+1 = 역들 사이 1 표준편차).
    과산포는 피어슨 χ²로 키운다(quasi-포아송). 반환: x가 1 표준편차 높을 때의 비율비와 80% 범위."""
    ok = exp > 0
    y, off, x = obs[ok].astype(float), np.log(exp[ok]), x[ok].astype(float)
    if len(y) < 4 or np.std(x) < 1e-9:
        return {"rr": math.nan, "lo": math.nan, "hi": math.nan}
    X = np.column_stack([np.ones_like(x), x])
    beta = np.array([math.log(max(y.sum(), 0.5) / np.exp(off).sum()), 0.0])
    for _ in range(50):
        mu = np.exp(off + X @ beta)
        grad = X.T @ (y - mu)
        H = X.T @ (X * mu[:, None])
        step = np.linalg.solve(H, grad)
        beta += step
        if np.abs(step).max() < 1e-8:
            break
    mu = np.exp(off + X @ beta)
    cov = np.linalg.inv(X.T @ (X * mu[:, None]))
    disp = max(1.0, float(((y - mu) ** 2 / mu).sum() / max(len(y) - 2, 1)))
    se = math.sqrt(cov[1, 1] * disp)
    rr = math.exp(beta[1])
    return {"rr": rr, "lo": math.exp(beta[1] - Z80 * se), "hi": math.exp(beta[1] + Z80 * se), "dispersion": disp}


def verdict(r: dict) -> str:
    """80% 범위로 말하는 판정."""
    if not r or not np.isfinite(r.get("rr", math.nan)):
        return "모름"
    if r["lo"] > 1:
        return "잦음"
    if r["hi"] < 1:
        return "드묾"
    return "차이 모름"


# ---------- 패널: 역 × 소분류 × 날의 실제·기대 고장 ----------


@dataclass
class Panel:
    day0: date
    days: list[date]
    station_ids: list[int]
    station_names: dict[int, str]
    codes: list[str]
    O: np.ndarray  # [역, 소분류, 날] 실제 고장(사후조치) 수
    E: np.ndarray  # [역, 소분류, 날] 기대 고장
    n_parts: np.ndarray  # [역, 소분류]
    door_obs: dict[int, np.ndarray]  # 문 → [소분류] 실제 고장
    door_info: dict[int, tuple[int, str]]  # 문 → (역, 위치 이름)
    weather: dict[str, np.ndarray] = field(default_factory=dict)  # 묶음·평년 대비 [역, 날]
    cond: dict[str, np.ndarray] = field(default_factory=dict)  # 날씨 조건 [역, 날] bool
    filled: np.ndarray | None = None  # [역, 날] 날씨 자료 밖이라 다른 해 같은 날로 채움
    holidays: dict[date, str] = field(default_factory=dict)
    rates: dict = field(default_factory=dict)  # (소분류, 나이 구간) → 하루 고장률

    @property
    def month(self) -> np.ndarray:
        return np.array([d.month for d in self.days])

    @property
    def weekday(self) -> np.ndarray:
        return np.array([d.weekday() for d in self.days])

    @property
    def ym(self) -> np.ndarray:
        return np.array([d.year * 12 + d.month - 1 for d in self.days])

    @property
    def is_holiday(self) -> np.ndarray:
        return np.array([d in self.holidays for d in self.days])


def _band(years: float) -> int:
    return sum(years >= b for b in AGE_BANDS)


def build_panel(session) -> Panel:
    """DB 기록으로 패널을 만든다(수 초)."""
    from psd.calibrate import PSD_START
    from psd.catalog import PART_TYPES
    from psd.models import Door
    from psd.risk import FeatureBuilder
    from psd.stations import STATIONS

    fb = FeatureBuilder.load(session)
    stations = [s for s in STATIONS if s.in_service]
    sidx = {s.id: i for i, s in enumerate(stations)}
    codes = [p["code"] for p in PART_TYPES]
    cidx = {c: i for i, c in enumerate(codes)}
    parts = [p for p in fb.parts if p.station_id in sidx and p.code in cidx]
    day0 = min(p.start for p in parts)
    day_end = fb.data_end()
    D = (day_end - day0).days + 1
    S, C = len(stations), len(codes)
    O = np.zeros((S, C, D), dtype=np.float32)
    n_parts = np.zeros((S, C), dtype=int)
    door_obs: dict[int, np.ndarray] = {}

    # 1) 날 → 달 번호, 달마다 첫 날 번호
    days = [day0 + timedelta(days=i) for i in range(D)]
    month_of_day = np.array([(d.year - day0.year) * 12 + d.month - day0.month for d in days])
    M = int(month_of_day[-1]) + 1
    month_first = np.searchsorted(month_of_day, np.arange(M))  # 그 달 첫 날(자료 첫 달은 day0)
    nb = len(AGE_BANDS) + 1
    expo = np.zeros((C, nb))
    fails = np.zeros((C, nb))
    part_bands: list[tuple[int, int, np.ndarray, np.ndarray]] = []  # (역, 소분류, 달마다 나이 구간, 달마다 노출 일수)
    for p in parts:
        s, c = sidx[p.station_id], cidx[p.code]
        n_parts[s, c] += 1
        start = (p.start - day0).days
        fail_days = np.array([(d - day0).days for d, r, _a in p.events if r == "fail" and p.start <= d <= day_end], dtype=int)
        full_days = np.array([(d - day0).days for d, _r, a in p.events if a == "full" and p.start <= d <= day_end], dtype=int)
        np.add.at(O[s, c], fail_days, 1)
        if p.door_id is not None and len(fail_days):
            door_obs.setdefault(p.door_id, np.zeros(C))[c] += len(fail_days)
        # 달마다: 노출 일수(사용개시 뒤), 그 달 첫 노출 날 기준 마지막 전체 교체 뒤 나이 → 구간
        first = np.maximum(month_first, start)
        last = np.append(month_first[1:] - 1, D - 1)
        n_days = np.clip(last - first + 1, 0, None)
        k = np.searchsorted(full_days, first, side="left")  # 그 달 첫 날 전에 있었던 전체 교체 수
        last_full = np.where(k > 0, full_days[np.maximum(k - 1, 0)] if len(full_days) else start, start)
        band = np.minimum(((first - last_full) / 365.25).astype(int), nb - 1).clip(0)
        n_fail = np.bincount(month_of_day[fail_days], minlength=M) if len(fail_days) else np.zeros(M)
        live = n_days > 0
        np.add.at(expo[c], band[live], n_days[live])
        np.add.at(fails[c], band[live], n_fail[live])
        part_bands.append((s, c, band, n_days))

    # 2) 노선 전체 소분류 × 나이 구간 하루 고장률 → 기대 고장(그 달 노출 날에 고르게)
    rate = np.divide(fails, expo, out=np.zeros_like(fails), where=expo > 0)
    E_month = np.zeros((S, C, M))
    days_month = np.zeros((S, M))
    for s, c, band, n_days in part_bands:
        E_month[s, c] += rate[c, band] * n_days
        days_month[s] = np.maximum(days_month[s], n_days)  # 역 안 부품은 사용개시가 같다
    per_day = np.divide(E_month, days_month[:, None, :], out=np.zeros_like(E_month), where=days_month[:, None, :] > 0)
    starts = np.array([(PSD_START[s.id] - day0).days for s in stations])
    E = per_day[:, :, month_of_day].astype(np.float32)
    E[:, :, :] *= (np.arange(D)[None, :] >= starts[:, None])[:, None, :]

    doors = {d.id: (sidx.get(d.station_id, -1), d.label) for d in session.query(Door).all()}
    panel = Panel(day0, days, [s.id for s in stations],
                  {s.id: s.name for s in stations}, codes, O, E, n_parts, door_obs,
                  {k: v for k, v in doors.items() if v[0] >= 0},
                  holidays=load_holidays(),
                  rates={(codes[c], b): float(rate[c, b]) for c in range(C) for b in range(nb)})
    _attach_weather(panel, stations)
    return panel


def _attach_weather(panel: Panel, stations) -> None:
    """역마다 AWS 지점의 날씨 묶음·평년 대비·날씨 조건을 날마다 붙인다(자료 밖의 날은 가장 가까운 해 같은 날)."""
    import pandas as pd

    from psd.cycle import load_aws_weather
    from psd.weather import BUNDLES, _source_day, daily_stress

    stress = daily_stress()
    raw = load_aws_weather()
    raw["date"] = raw["date"].dt.date
    S, D = len(stations), len(panel.days)
    first, last = min(stress["date"]), max(stress["date"])
    src = [d if first <= d <= last else _source_day(d, first, last) for d in panel.days]
    panel.filled = np.zeros((S, D), dtype=bool)
    for name in [*BUNDLES, *(b + "_anom" for b in BUNDLES)]:
        panel.weather[name] = np.full((S, D), np.nan)
    for key in CONDITIONS:
        panel.cond[key] = np.zeros((S, D), dtype=bool)
    for i, st in enumerate(stations):
        g = stress[stress["stn"] == st.aws_stn].set_index("date")
        r = raw[raw["stn"] == st.aws_stn].set_index("date")
        if g.empty:
            continue
        gi = g.reindex(src)
        for name in panel.weather:
            panel.weather[name][i] = gi[name].to_numpy(dtype=float)
        ri = r.reindex(src)
        for key, (_label, rule) in CONDITIONS.items():
            panel.cond[key][i] = rule(ri.fillna(0)).to_numpy(dtype=bool)
        panel.filled[i] = np.array([s != d for s, d in zip(src, panel.days)])
    panel.weather = {k: pd.DataFrame(v).ffill(axis=1).bfill(axis=1).to_numpy() for k, v in panel.weather.items()}


def clean(x):
    """화면(JSON)으로 보낼 수 있게 numpy 값을 파이썬 값으로, NaN은 None으로."""
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if isinstance(x, np.generic):
        x = x.item()
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


# ---------- 캐시 ----------

_CACHE: dict = {}


def get_panel(session) -> Panel:
    """기록이 바뀌지 않았으면 만든 패널을 다시 쓴다(data_key = 재학습과 같은 열쇠)."""
    from psd.risk import data_key

    key = data_key(session)
    if _CACHE.get("key") != key:
        _CACHE.update(key=key, panel=build_panel(session), results={})
    return _CACHE["panel"]


def cached(name: str, args: tuple, fn):
    """같은 패널·같은 조건의 결과를 다시 쓴다(소분류별 날씨 표처럼 몇 초 걸리는 것)."""
    res = _CACHE.setdefault("results", {})
    k = (name, args)
    if k not in res:
        if len(res) > 200:
            res.clear()
        res[k] = fn()
    return res[k]


# ---------- 고르기 ----------


def code_mask(panel: Panel, group: str | None = None, code: str | None = None) -> np.ndarray:
    from psd.catalog import PART_TYPES

    by = {p["code"]: p for p in PART_TYPES}
    m = np.ones(len(panel.codes), dtype=bool)
    if code:
        m &= np.array([c == code for c in panel.codes])
    if group:
        m &= np.array([group_of(by[c]) == group for c in panel.codes])
    return m


def group_of(p: dict) -> str:
    return (p.get("group") or "").replace(" 장애", "")


def day_mask(panel: Panel, months: list[int] | None = None, season: str | None = None, daytype: str | None = None,
             holiday: str | None = None) -> np.ndarray:
    """날 거르기: 달, 계절, 평일/주말, 공휴일만/공휴일 빼고."""
    m = np.ones(len(panel.days), dtype=bool)
    mon, wd, hol = panel.month, panel.weekday, panel.is_holiday
    if months:
        m &= np.isin(mon, months)
    if season in SEASONS:
        m &= np.isin(mon, SEASONS[season])
    if daytype == "weekday":
        m &= wd < 5
    elif daytype == "weekend":
        m &= wd >= 5
    if holiday == "only":
        m &= hol
    elif holiday == "exclude":
        m &= ~hol
    return m


def _sel(panel: Panel, cm: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """소분류를 골라 더한 [역, 날] 실제·기대."""
    return panel.O[:, cm, :].sum(axis=1, dtype=np.float64), panel.E[:, cm, :].sum(axis=1, dtype=np.float64)


# ---------- 질문 1: 어느 역이 ----------


def station_ranking(panel: Panel, cm: np.ndarray, dm: np.ndarray) -> dict:
    """역 순위(당긴 고장 비율, 80% 범위)와 역 날씨로 설명하기."""
    from psd.weather import BUNDLES

    O, E = _sel(panel, cm)
    obs, exp = O[:, dm].sum(axis=1), E[:, dm].sum(axis=1)
    k = shrink_strength(obs, exp)
    rows = []
    for i, sid in enumerate(panel.station_ids):
        m, lo, hi = gamma_ci(obs[i], exp[i], k)
        raw = obs[i] / exp[i] if exp[i] > 0 else math.nan
        rows.append({"station_id": sid, "station_name": panel.station_names[sid], "obs": int(obs[i]), "exp": round(float(exp[i]), 1),
                     "raw_ratio": round(raw, 3) if np.isfinite(raw) else None, "ratio": round(m, 3), "lo": round(lo, 3), "hi": round(hi, 3),
                     "verdict": verdict({"rr": m, "lo": lo, "hi": hi})})
    rows.sort(key=lambda r: -(r["ratio"] if np.isfinite(r["ratio"]) else -1))
    # 역 날씨(그 역 평소 묶음 점수)로 설명 — 묶음마다 따로(역 날씨가 7개 지점뿐이라 함께 넣으면 구분이 안 된다)
    explain = []
    for b, label in BUNDLES.items():
        prof = np.array([np.nanmean(panel.weather[b][i, dm]) for i in range(len(panel.station_ids))])
        z = (prof - prof.mean()) / prof.std() if prof.std() > 0 else prof * 0
        r = poisson_rr(obs, exp, z)
        explain.append({"bundle": b, "label": label, **{kk: (round(v, 3) if isinstance(v, float) and np.isfinite(v) else None) for kk, v in r.items()},
                        "verdict": verdict(r),
                        "top": [panel.station_names[panel.station_ids[i]] for i in np.argsort(-prof)[:3]]})
    explain += _env_explain(panel, obs, exp)
    return {"stations": rows, "shrink": round(k, 1), "explain": explain, "total_obs": int(obs.sum()), "total_exp": round(float(exp.sum()), 1),
            "weather_points": len({tuple(np.round(panel.weather["humid"][i, :50], 6)) for i in range(len(panel.station_ids))})}


def _env_explain(panel: Panel, obs: np.ndarray, exp: np.ndarray) -> list[dict]:
    """역환경 파일(역 구조·외기 노출 등 '선택' 항목)이 채워져 있으면 그 값으로도 설명한다. 값이 2가지 이상, 역 3곳 이상일 때만."""
    from psd.stations import environment

    env = environment()
    out = []
    for f in env.fields:
        if f.kind != "선택":
            continue
        vals = [env.values.get(sid, {}).get(f.name) for sid in panel.station_ids]
        cats = sorted({v for v in vals if v})
        if len(cats) < 2:
            continue
        for cat in cats:
            x = np.array([1.0 if v == cat else 0.0 for v in vals])
            known = np.array([v is not None and v != "" for v in vals])
            if x[known].sum() < 3 or (1 - x[known]).sum() < 3:
                continue
            r = poisson_rr(obs[known], exp[known], x[known])
            out.append({"bundle": f"env:{f.name}", "label": f"{f.name} = {cat}", **{kk: (round(v, 3) if isinstance(v, float) and np.isfinite(v) else None)
                        for kk, v in r.items()}, "verdict": verdict(r), "kind": "env"})
    return out


def station_detail(panel: Panel, station_id: int, cm: np.ndarray, dm: np.ndarray) -> dict:
    """한 역: 소분류별 고장 비율(노선 평균 쪽으로 당김), 그 소분류가 잦은 문."""
    from psd.catalog import PART_TYPES

    by = {p["code"]: p for p in PART_TYPES}
    i = panel.station_ids.index(station_id)
    obs_sc, exp_sc = panel.O[:, :, dm].sum(axis=2, dtype=np.float64), panel.E[:, :, dm].sum(axis=2, dtype=np.float64)
    parts = []
    for c, code in enumerate(panel.codes):
        if not cm[c] or panel.n_parts[i, c] == 0:
            continue
        k = shrink_strength(obs_sc[:, c], exp_sc[:, c])
        m, lo, hi = gamma_ci(obs_sc[i, c], exp_sc[i, c], k)
        parts.append({"code": code, "name": by[code]["name"], "group": group_of(by[code]), "unit": by[code]["unit"],
                      "obs": int(obs_sc[i, c]), "exp": round(float(exp_sc[i, c]), 2), "ratio": round(m, 3), "lo": round(lo, 3), "hi": round(hi, 3),
                      "verdict": verdict({"rr": m, "lo": lo, "hi": hi})})
    parts.sort(key=lambda r: (-r["lo"], -r["obs"]))  # 확실히 잦은 것(80% 범위 아래쪽이 높은 것) 먼저
    # 문: 그 역 문마다 고른 소분류의 실제 고장(전 기간) — 기대는 문마다 같다(문 하나에 소분류 하나씩)
    exp_door = np.where(panel.n_parts[i] > 0, panel.E[i].sum(axis=1, dtype=np.float64) / np.maximum(panel.n_parts[i], 1), 0)
    door_cm = cm & np.array([by[c]["unit"] == "door" for c in panel.codes])
    doors = []
    for did, (si, label) in panel.door_info.items():
        if si != i:
            continue
        o = panel.door_obs.get(did, np.zeros(len(panel.codes)))
        ob, ex = float(o[door_cm].sum()), float(exp_door[door_cm].sum())
        top = [panel.codes[c] for c in np.argsort(-o) if door_cm[c] and o[c] > 0][:3]
        doors.append({"door_id": did, "label": label, "obs": int(ob), "exp": round(ex, 2), "ratio": round(ob / ex, 2) if ex > 0 else None,
                      "top_parts": [{"code": c, "name": by[c]["name"], "obs": int(o[panel.codes.index(c)])} for c in top]})
    doors.sort(key=lambda r: (-r["obs"], r["label"]))
    return {"station_id": station_id, "station_name": panel.station_names[station_id], "parts": parts, "doors": doors[:12],
            "doors_note": "문 고장 수는 전 기간(날 거르기와 상관없음), 기대는 문 하나의 평균"}


# ---------- 질문 2: 언제 ----------


def when(panel: Panel, cm: np.ndarray, dm: np.ndarray, station_id: int | None = None, extras: bool = True) -> dict:
    """달별·계절별·요일별 고장 비율, 날씨 조건 비율비(같은 역·같은 달 안), 평년보다 습했던 달 등(같은 역·같은 달 다른 해),
    요일·공휴일 비율비, 역 × 계절 특이."""
    from psd.weather import BUNDLES

    O, E = _sel(panel, cm)
    rows = np.arange(len(panel.station_ids)) if station_id is None else np.array([panel.station_ids.index(station_id)])
    O, E = O[rows], E[rows]
    S, D = O.shape
    mon, wd, ym, hol = panel.month, panel.weekday, panel.ym, panel.is_holiday
    dmS = np.broadcast_to(dm, (S, D))

    def ratio_by(keys: np.ndarray, values) -> list[dict]:
        out = []
        for v in values:
            m = dmS & (keys == v)
            ob, ex = float(O[m].sum()), float(E[m].sum())
            r, lo, hi = gamma_ci(ob, ex)
            out.append({"key": v, "obs": int(ob), "exp": round(ex, 1), "ratio": round(r, 3) if np.isfinite(r) else None,
                        "lo": round(lo, 3) if np.isfinite(lo) else None, "hi": round(hi, 3) if np.isfinite(hi) else None})
        return out

    monB, wdB = np.broadcast_to(mon, (S, D)), np.broadcast_to(wd, (S, D))
    by_month = ratio_by(monB, range(1, 13))
    season_key = np.vectorize(lambda m: next(k for k, v in SEASONS.items() if m in v))(monB)
    by_season = ratio_by(season_key, list(SEASONS))
    by_weekday = ratio_by(wdB, range(7))
    for r in by_weekday:
        r["label"] = WEEKDAYS[r["key"]]

    # 층: 역 × 해-달(같은 역·같은 달 안에서 비교)
    strata = np.arange(S)[:, None] * 100000 + ym[None, :]
    Om, Em = O[dmS], E[dmS]
    st_m = strata[dmS]

    def rr(X: np.ndarray) -> dict:
        r = mh_rate_ratio(Om, Em, X[dmS], st_m)
        return {**{k: (round(v, 3) if isinstance(v, float) and np.isfinite(v) else (v if not isinstance(v, float) else None)) for k, v in r.items()},
                "verdict": verdict(r)}

    conditions = []
    for key, (label, _rule) in CONDITIONS.items():
        c = panel.cond[key][rows]
        window = np.zeros_like(c)  # 그 날 ~ 일주일 뒤
        for lag in range(LAG_DAYS + 1):
            window[:, lag:] |= c[:, :D - lag] if lag else c
        conditions.append({"key": key, "label": label, "days_share": round(float(c[dmS].mean()), 3),
                           "same_day": rr(c), "week": rr(window)})
    # 평년보다 습한(더운 …) 날: 같은 역·같은 달 안에서 평년 대비가 높은 날 대 낮은 날. 날씨가 나이 먹는 속도를 바꾸면
    # 그날 고장 확률이 바로 달라지므로 가장 민감한 비교다(지난 7일 평균도 함께 — 며칠 늦게 나타나는 영향)
    daily_weather = []
    for b, label in BUNDLES.items():
        an = panel.weather[b + "_anom"][rows]
        avg7 = np.copy(an)
        for lag in range(1, LAG_DAYS):
            avg7[:, lag:] += an[:, :D - lag]
        avg7 /= LAG_DAYS
        daily_weather.append({"bundle": b, "label": f"평년보다 {label} 높은 날 (같은 달 다른 날 대비)", "same_day": rr(an > 0), "week": rr(avg7 > 0)})
    calendar = [{"key": "weekend", "label": "토·일 (평일 대비)", **rr(np.broadcast_to(wd >= 5, (S, D)))},
                {"key": "holiday", "label": "공휴일 (다른 날 대비)", **rr(np.broadcast_to(hol, (S, D)))}]

    # 평년보다 습했던(더웠던 …) 달: 역 × 해-달로 모아 위 1/3 대 아래 1/3, 층 = 역 × 달(1~12월, 다른 해끼리)
    monthly = []
    cell = np.arange(S)[:, None] * 100000 + np.broadcast_to(ym, (S, D))
    cid, inv = np.unique(cell[dmS], return_inverse=True)
    Oc = np.bincount(inv, O[dmS])
    Ec = np.bincount(inv, E[dmS])
    s_of = cid // 100000
    moy = (cid % 100000) % 12 + 1
    for b, label in BUNDLES.items():
        an = panel.weather[b + "_anom"][rows][dmS]
        Ac = np.bincount(inv, an) / np.maximum(np.bincount(inv), 1)
        lo_q, hi_q = np.quantile(Ac, [1 / 3, 2 / 3])
        keep = (Ac <= lo_q) | (Ac >= hi_q)
        r = mh_rate_ratio(Oc[keep], Ec[keep], (Ac >= hi_q)[keep], (s_of * 100 + moy)[keep])
        monthly.append({"bundle": b, "label": f"평년보다 {label} 높았던 달 (낮았던 달 대비)",
                        **{k: (round(v, 3) if isinstance(v, float) and np.isfinite(v) else (v if not isinstance(v, float) else None)) for k, v in r.items()},
                        "verdict": verdict(r)})

    out = {"by_month": by_month, "by_season": by_season, "by_weekday": by_weekday, "conditions": conditions, "calendar": calendar,
           "daily_weather": daily_weather, "monthly_weather": monthly, "obs": int(Om.sum()), "exp": round(float(Em.sum()), 1)}
    if station_id is None and extras:
        out["heatmap"] = heatmap(panel, cm, dm)
        out["station_season"] = station_season(panel, cm, dm)
    return out


def heatmap(panel: Panel, cm: np.ndarray, dm: np.ndarray) -> list[dict]:
    """역 × 달(1~12월) 고장 비율 — 역마다 노선 평균 쪽으로 당김."""
    O, E = _sel(panel, cm)
    mon = panel.month
    out = []
    for i, sid in enumerate(panel.station_ids):
        cells = []
        for m in range(1, 13):
            msk = dm & (mon == m)
            ob, ex = float(O[i, msk].sum()), float(E[i, msk].sum())
            r, _lo, _hi = gamma_ci(ob, ex, 5.0)
            cells.append({"month": m, "obs": int(ob), "exp": round(ex, 1), "ratio": round(r, 2) if np.isfinite(r) else None})
        out.append({"station_id": sid, "station_name": panel.station_names[sid], "months": cells})
    return out


def station_season(panel: Panel, cm: np.ndarray, dm: np.ndarray) -> list[dict]:
    """③ 날씨 아닌 것의 후보: "이 역만 그 계절에 유독" — 역의 계절 비율 ÷ 노선 전체의 같은 계절 비율.
    (그 역의 계절 고장 비율 / 그 역의 다른 계절) ÷ (노선 전체 같은 비). 1보다 확실히 크면 이용객 등 그 역 사정을 의심한다."""
    O, E = _sel(panel, cm)
    mon = panel.month
    out = []
    for season, months in SEASONS.items():
        ins = dm & np.isin(mon, months)
        oth = dm & ~np.isin(mon, months)
        line = (O[:, ins].sum() / max(E[:, ins].sum(), 1e-9)) / (O[:, oth].sum() / max(E[:, oth].sum(), 1e-9))
        for i, sid in enumerate(panel.station_ids):
            o1, e1, o0, e0 = O[i, ins].sum(), E[i, ins].sum(), O[i, oth].sum(), E[i, oth].sum()
            if min(o1, o0) < 3 or min(e1, e0) <= 0:
                continue
            rr = float((o1 / e1) / (o0 / e0) / line)
            se = math.sqrt(1 / float(o1) + 1 / float(o0))
            r = {"rr": rr, "lo": rr * math.exp(-Z80 * se), "hi": rr * math.exp(Z80 * se)}
            if r["lo"] > 1:
                out.append({"season": season, "station_id": sid, "station_name": panel.station_names[sid], "obs": int(o1),
                            "rr": round(float(rr), 3), "lo": round(float(r["lo"]), 3), "hi": round(float(r["hi"]), 3)})
    out.sort(key=lambda r: -r["lo"])
    return out


def part_weather_table(panel: Panel, dm: np.ndarray) -> list[dict]:
    """장애부위(소분류)마다 날씨 영향: 평년보다 습했던(더웠던·추웠던·바람 센) 달 비율비와 날씨 조건 뒤 일주일 비율비.
    교체주기 모델의 날씨 민감도(설계 3장)와 함께 보는 "어느 부품이 날씨를 타나" 표."""
    from psd.catalog import PART_TYPES

    by = {p["code"]: p for p in PART_TYPES}
    out = []
    for c, code in enumerate(panel.codes):
        cm = np.zeros(len(panel.codes), dtype=bool)
        cm[c] = True
        if panel.O[:, c, :][:, dm].sum() < 20:  # 고장이 너무 적으면 재지 않는다
            out.append({"code": code, "name": by[code]["name"], "group": group_of(by[code]), "obs": int(panel.O[:, c, :][:, dm].sum()),
                        "too_few": True})
            continue
        w = when(panel, cm, dm, extras=False)
        out.append({"code": code, "name": by[code]["name"], "group": group_of(by[code]), "obs": w["obs"],
                    "daily": {m["bundle"]: {k: m["same_day"][k] for k in ("rr", "lo", "hi", "verdict")} for m in w["daily_weather"]},
                    "monthly": {m["bundle"]: {k: m[k] for k in ("rr", "lo", "hi", "verdict")} for m in w["monthly_weather"]},
                    "week": {x["key"]: {k: x["week"][k] for k in ("rr", "lo", "hi", "verdict")} for x in w["conditions"]},
                    "summer": next((s["ratio"] for s in w["by_season"] if s["key"] == "여름"), None),
                    "weekend": {k: w["calendar"][0][k] for k in ("rr", "lo", "hi", "verdict")}})
    return out
