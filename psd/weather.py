"""날씨 묶음 4개 — 습기·열·추위·바람의 하루 스트레스 점수 (2026-11-27, 날씨_역분석_엑셀연동_설계.md 0장).

현장 의견("습도가 중요한 것 같다")을 데이터로 확인하고, 모든 소분류에 날씨를 넣기 위한 공통 재료다.
비슷하게 움직이는 항목(여름의 기온·습도·폭염)을 하나씩 넣으면 24개 역·4년치로는 무엇 때문인지 구분이 안 되어,
원인별 묶음 4개로 모은다.

| 묶음 | 재료 (scripts/preprocess_aws_weather.py가 시간 자료에서 만든 하루 값) |
|---|---|
| humid 습기 | 습도 부식 계수(af_hum, Peck), 결로 가능 시간, 비 온 시간, 시간당 최대 강수 |
| heat 열   | 고온 계수(af_cap, 10℃마다 2배), 일교차 계수(af_sol, Norris-Landzberg), 30℃ 이상 시간 |
| cold 추위 | 영하 시간, 얼었다 녹은 횟수 |
| wind 바람 | 최대 풍속, 강풍(8m/s 이상) 시간, 비바람 시간 |

점수 만드는 법:
1. 재료마다 "기준 평균"으로 나눈다. 기준 평균 = 동해선 역들이 쓰는 AWS 지점의 2022~2025 평균을, 지점에 연결된 역 수로
   가중한 것(psd/cycle.py reference_climate와 같은 방식). 그래서 동해선의 평소 하루가 1이다.
2. 드문 날(태풍·한파)이 한 날에 수십 배가 되지 않게 재료마다 CAP(5배)로 자른다.
3. 묶음 점수 = 재료 비율의 평균을 다시 기준 평균으로 나눈 값. 평소 하루 = 1, 0이면 그 스트레스가 전혀 없는 날
   (예: 여름의 추위), 2면 평소의 두 배.

평년값: 지점·날짜마다 4년의 같은 철(앞뒤 15일) 평균. "평년 대비"(값 − 평년)는 계절 효과와 날씨 효과를 나누는 데 쓴다
(같은 여름이라도 유난히 습했는가).

날씨 자료 밖의 날(지금은 2026년 이후)은 가장 가까운 해의 같은 날 값으로 채우고 filled=True로 표시한다
(scripts/generate_dummy_records.py의 날씨 채우기와 같은 규칙). 2026년 자료를 받으면 채운 값이 실제 값으로 바뀐다.

알아둘 점:
- 바람은 AWS 지점 위치를 많이 탄다. 간절곶(서생역)은 곶 끝이라 강풍 시간이 하루 1.3시간, 동래·기장은 거의 0이다.
  역 승강장 바람과 다를 수 있다 — 역 차이로 바람 영향을 말할 때는 이 점을 같이 밝힌다.
- 역 구조(지하·밀폐형이면 비·바람·추위를 덜 받음)는 여기서 반영하지 않는다. 얼마나 덜 받는지는 모델이 배운다
  (역환경 파일이 채워진 뒤).
"""
from __future__ import annotations

from datetime import date
from functools import lru_cache

import numpy as np
import pandas as pd

from psd.cycle import daily_af, load_aws_weather, reference_climate, station_counts

BUNDLES: dict[str, str] = {"humid": "습기", "heat": "열", "cold": "추위", "wind": "바람"}
COMPONENTS: dict[str, tuple[str, ...]] = {
    "humid": ("af_hum", "cond_hours", "rn_hours", "rn_max"),
    "heat": ("af_cap", "af_sol", "hot_hours"),
    "cold": ("frz_hours", "thaw_count"),
    "wind": ("ws_max", "wind_hours", "rain_wind_hours"),
}
COMPONENT_LABELS: dict[str, str] = {
    "af_hum": "습도 부식 계수", "cond_hours": "결로 가능 시간", "rn_hours": "비 온 시간", "rn_max": "시간당 최대 강수",
    "af_cap": "고온 계수", "af_sol": "일교차 계수", "hot_hours": "30℃ 이상 시간",
    "frz_hours": "영하 시간", "thaw_count": "얼었다 녹은 횟수",
    "ws_max": "최대 풍속", "wind_hours": "강풍 시간", "rain_wind_hours": "비바람 시간",
}
CAP = 5.0  # 재료 비율 상한(드문 날이 한 날에 수십 배가 되지 않게)
CLIM_HALF_WINDOW = 15  # 평년값: 앞뒤 15일


def _weights(stns: pd.Series) -> pd.Series:
    """지점마다 연결된 역 수(기준 평균의 가중치). 동해선이 안 쓰는 지점은 0."""
    counts = station_counts()
    return stns.map(lambda s: counts.get(s, 0)).astype(float)


def _ref_mean(values: pd.Series, w: pd.Series) -> float:
    ok = values.notna() & (w > 0)
    if not ok.any():
        raise ValueError("기준 평균을 낼 날씨 값이 없습니다")
    return float(np.average(values[ok], weights=w[ok]))


def stress_from_daily(daily: pd.DataFrame) -> pd.DataFrame:
    """AWS 일별 표(daily_af를 거친 것, 모든 재료 열 포함) → 지점·날짜마다 묶음 점수.

    반환 열: date, stn, humid, heat, cold, wind (+ 재료 비율 r_<재료>). 동해선이 쓰는 지점만 남긴다.
    """
    used = daily[_weights(daily["stn"]) > 0].copy()
    w = _weights(used["stn"])
    out = used[["date", "stn"]].copy()
    for bundle, comps in COMPONENTS.items():
        ratios = []
        for c in comps:
            ref = _ref_mean(used[c].astype(float), w)
            r = (used[c].astype(float) / ref).clip(upper=CAP) if ref > 0 else pd.Series(0.0, index=used.index)
            out["r_" + c] = r
            ratios.append(r)
        raw = pd.concat(ratios, axis=1).mean(axis=1, skipna=True)  # 재료 하나가 결측이면 나머지로(모두 결측이면 NaN)
        out[bundle] = raw / _ref_mean(raw, w)
    return out.reset_index(drop=True)


def climatology(stress: pd.DataFrame) -> pd.DataFrame:
    """지점 × 날짜마다 평년값(4년의 같은 철, 앞뒤 CLIM_HALF_WINDOW일 평균)과 평년 대비(값 − 평년)를 붙인다."""
    df = stress.copy()
    doy = pd.to_datetime(df["date"]).dt.dayofyear.clip(upper=365)  # 윤년 12/31은 365로
    df["_doy"] = doy
    for b in BUNDLES:
        clim_col = np.full(len(df), np.nan)
        for stn, g in df.groupby("stn"):
            by_doy = g.groupby("_doy")[b].mean().reindex(range(1, 366))
            # 원형(12월 말과 1월 초가 이어지게)으로 이동 평균
            arr = by_doy.to_numpy()
            ext = np.concatenate([arr[-CLIM_HALF_WINDOW:], arr, arr[:CLIM_HALF_WINDOW]])
            smooth = pd.Series(ext).rolling(2 * CLIM_HALF_WINDOW + 1, center=True, min_periods=1).mean()
            smooth = smooth.to_numpy()[CLIM_HALF_WINDOW:-CLIM_HALF_WINDOW]
            clim_col[g.index] = smooth[g["_doy"].to_numpy() - 1]
        df[b + "_clim"] = clim_col
        df[b + "_anom"] = df[b] - df[b + "_clim"]
    return df.drop(columns="_doy")


@lru_cache(maxsize=1)
def daily_stress() -> pd.DataFrame:
    """동해선이 쓰는 AWS 지점의 날짜별 묶음 점수·평년값·평년 대비 (data/processed/aws_weather_daily.csv에서)."""
    weather = load_aws_weather()
    missing = [c for comps in COMPONENTS.values() for c in comps if not c.startswith("af_") and c not in weather.columns]
    if missing:
        raise ValueError(f"날씨 파일에 {missing} 열이 없습니다 — python scripts/preprocess_aws_weather.py를 다시 돌리세요")
    daily = daily_af(weather, reference_climate(weather))
    stress = climatology(stress_from_daily(daily))
    stress["date"] = pd.to_datetime(stress["date"]).dt.date
    stress["filled"] = False
    return stress


def _source_day(d: date, first: date, last: date) -> date:
    """날씨 자료 밖의 날 → 가장 가까운 해의 같은 날(2/29는 2/28)."""
    year = min(max(d.year, first.year), last.year)
    md = (d.month, 28) if (d.month, d.day) == (2, 29) else (d.month, d.day)
    src = date(year, *md)
    if src > last:
        src = date(year - 1, *md)
    if src < first:
        src = date(year + 1, *md)
    return src


def stress_for(stn: str, days: list[date]) -> pd.DataFrame:
    """한 지점의 여러 날짜 점수. 자료 밖의 날은 가장 가까운 해의 같은 날로 채우고 filled=True."""
    all_ = daily_stress()
    g = all_[all_["stn"] == str(stn)].set_index("date")
    if g.empty:
        raise ValueError(f"AWS 지점 {stn}의 날씨가 없습니다")
    first, last = min(g.index), max(g.index)
    rows = []
    for d in days:
        src = d if first <= d <= last and d in g.index else _source_day(d, first, last)
        row = g.loc[src].to_dict()
        row.update(date=d, stn=str(stn), filled=src != d)
        rows.append(row)
    return pd.DataFrame(rows)


def station_profile() -> pd.DataFrame:
    """지점별 평균 묶음 점수(역 차이를 한눈에). 행 = 지점, 열 = 묶음."""
    s = daily_stress()
    return s.groupby("stn")[list(BUNDLES)].mean()
