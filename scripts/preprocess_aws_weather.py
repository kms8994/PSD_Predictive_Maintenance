"""AWS(방재기상관측) 시간 자료에서 일별 값을 만든다 (설계_v2.md 2-6 "동 단위" 세분화).

기상자료개방포털(data.kma.go.kr) 파일셋에서 받은 시간 자료(`data/raw/aws/hourly/*.csv`,
`SURFACE_AWS_<지점>_HR_<연도>_<연도>_<생산연도>.csv`, euc-kr)를 지점·날짜별로 모아 하루
평균·최고·최저 기온과 평균 습도를 낸다. 열 이름은 예전 ASOS 일자료 전처리와 같게 맞춰 두었다 —
psd/cycle.py의 AF 계산이 그 열 이름을 그대로 쓴다.

AWS 시간 자료는 결측이 빈 문자열로 온다(예: 레이더 전용국은 기압이 항상 빈값). 하루 안에
기온·습도 관측이 하나도 없는 날만 결측으로 남기고, 나머지는 있는 값만으로 평균 낸다.

2026-11-27: 날씨 묶음(습기·열·추위·바람, psd/weather.py)을 위해 시간 자료에서 하루 값을 더 뽑는다
(날씨_역분석_엑셀연동_설계.md 0장). 기준은 2024년 시간 자료 분포를 보고 정했다(1시간 기준 비율):
- 강수: 하루 합(rn_sum), 시간당 최대(rn_max), 비 온 시간(rn_hours, 6.9%)
- 결로 가능 시간(cond_hours): 습도 95% 이상이거나, 지금 공기의 이슬점이 한 시간 전 기온보다 높은 시간
  (차가운 표면에 따뜻하고 습한 공기가 닿아 물이 맺히는 경우)
- 더운 시간(hot_hours): 30℃ 이상(2.9%) / 영하 시간(frz_hours, 2.8%) / 얼었다 녹은 횟수(thaw_count)
- 바람: 최대 풍속(ws_max), 강풍 시간(wind_hours, 풍속 8m/s 이상, 1.8%), 비바람 시간(rain_wind_hours, 비 + 풍속 5m/s 이상)
- 건조 시간(dry_hours): 습도 35% 이하(5.7%)

    python scripts/preprocess_aws_weather.py
"""
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from psd.cycle import AWS_STATION_NAMES as STATION_NAMES  # noqa: E402

RAW_DIR = ROOT / "data/raw/aws/hourly"
OUT = ROOT / "data/processed/aws_weather_daily.csv"

COLUMNS = ["stn", "datetime", "ta", "wd", "ws", "rn", "pa", "ps", "hm", "si", "ss"]


def load_raw() -> pd.DataFrame:
    files = sorted(RAW_DIR.glob("SURFACE_AWS_*_HR_*.csv"))
    if not files:
        raise SystemExit(f"{RAW_DIR}에 시간 자료 CSV가 없습니다. data/raw/aws/hourly/에 먼저 받아두세요.")
    parts = []
    for f in files:
        df = pd.read_csv(f, encoding="euc-kr", header=0, names=COLUMNS, dtype=str)
        parts.append(df)
    raw = pd.concat(parts, ignore_index=True)
    raw["stn"] = raw["stn"].astype(str)
    raw["datetime"] = pd.to_datetime(raw["datetime"], format="%Y-%m-%d %H:%M")
    raw["date"] = raw["datetime"].dt.normalize()
    for c in ("ta", "hm", "ws", "rn"):
        raw[c] = pd.to_numeric(raw[c], errors="coerce")
    return raw.drop_duplicates(["stn", "datetime"])


HOT_C = 30.0  # 더운 시간
WIND_MS = 8.0  # 강풍 시간
RAIN_WIND_MS = 5.0  # 비바람 시간의 풍속
COND_RH = 95.0  # 결로 가능 습도
DRY_RH = 35.0  # 건조 시간


def dew_point(ta: pd.Series, hm: pd.Series) -> pd.Series:
    """이슬점(℃), Magnus 식(a=17.62, b=243.12)."""
    import numpy as np

    rh = hm.clip(lower=1, upper=100) / 100
    g = np.log(rh) + 17.62 * ta / (243.12 + ta)
    return 243.12 * g / (17.62 - g)


def hourly_flags(g: pd.DataFrame) -> pd.DataFrame:
    """한 지점의 시간 자료(시간 순)에 시간마다 표시를 붙인다. 결측이면 False(세지 않음)."""
    g = g.sort_values("datetime").copy()
    prev_ta = g["ta"].shift(1).where(g["datetime"].diff() == pd.Timedelta(hours=1))
    td = dew_point(g["ta"], g["hm"])
    g["cond"] = (g["hm"] >= COND_RH) | (td > prev_ta)
    g["hot"] = g["ta"] >= HOT_C
    g["frz"] = g["ta"] < 0
    g["thaw"] = (prev_ta < 0) & (g["ta"] >= 0)
    g["rain"] = g["rn"] > 0
    g["windy"] = g["ws"] >= WIND_MS
    g["rain_wind"] = g["rain"] & (g["ws"] >= RAIN_WIND_MS)
    g["dry"] = g["hm"] <= DRY_RH
    return g


def daily_extra(g: pd.DataFrame) -> pd.DataFrame:
    """hourly_flags 결과를 날짜별로 모은다(시간 수·합·최대)."""
    return g.groupby("date").agg(
        rn_sum=("rn", "sum"),
        rn_max=("rn", "max"),
        rn_hours=("rain", "sum"),
        cond_hours=("cond", "sum"),
        hot_hours=("hot", "sum"),
        frz_hours=("frz", "sum"),
        thaw_count=("thaw", "sum"),
        ws_max=("ws", "max"),
        wind_hours=("windy", "sum"),
        rain_wind_hours=("rain_wind", "sum"),
        dry_hours=("dry", "sum"),
        ws_n=("ws", "count"),
        rn_n=("rn", "count"),
    )


EXTRA_COLUMNS = ["rn_sum", "rn_max", "rn_hours", "cond_hours", "hot_hours", "frz_hours", "thaw_count",
                 "ws_max", "wind_hours", "rain_wind_hours", "dry_hours"]


def main() -> None:
    raw = load_raw()

    out, report = [], {}
    for stn, g in raw.groupby("stn"):
        daily = g.groupby("date").agg(
            ta_avg=("ta", "mean"),
            ta_max=("ta", "max"),
            ta_min=("ta", "min"),
            hm_avg=("hm", "mean"),
            ta_n=("ta", "count"),
            hm_n=("hm", "count"),
        )
        daily = daily.join(daily_extra(hourly_flags(g)))
        full = pd.date_range(daily.index.min(), daily.index.max(), freq="D")
        daily = daily.reindex(full)
        daily.index.name = "date"
        # 그날 관측이 하나도 없으면(ta_n/hm_n이 0 또는 NaN) 결측으로 남긴다
        daily.loc[daily["ta_n"].fillna(0) == 0, ["ta_avg", "ta_max", "ta_min"]] = pd.NA
        daily.loc[daily["hm_n"].fillna(0) == 0, "hm_avg"] = pd.NA
        # 강수·바람 관측이 하나도 없는 날도 결측(0으로 세면 "비 안 온 날"로 잘못 읽힌다)
        daily.loc[daily["rn_n"].fillna(0) == 0, ["rn_sum", "rn_max", "rn_hours", "rain_wind_hours"]] = pd.NA
        daily.loc[daily["ws_n"].fillna(0) == 0, ["ws_max", "wind_hours", "rain_wind_hours"]] = pd.NA
        daily.loc[daily["ta_n"].fillna(0) == 0, ["hot_hours", "frz_hours", "thaw_count", "cond_hours"]] = pd.NA
        daily["dtr"] = daily["ta_max"] - daily["ta_min"]
        daily["frost"] = (daily["ta_min"] < 0).astype("Int64").where(daily["ta_min"].notna())
        daily["stn"] = stn
        daily["station_name"] = STATION_NAMES.get(stn, "")
        out.append(daily.reset_index())

        ok = daily.dropna(subset=["ta_avg", "hm_avg"])
        report[stn] = {
            "이름": STATION_NAMES.get(stn, ""),
            "기간": f"{full.min():%Y-%m-%d} ~ {full.max():%Y-%m-%d}",
            "전체 일수": len(daily),
            "결측일(기온 또는 습도)": int(len(daily) - len(ok)),
            "평균 기온": round(ok["ta_avg"].mean(), 2) if len(ok) else None,
            "평균 습도": round(ok["hm_avg"].mean(), 2) if len(ok) else None,
            "평균 일교차": round(ok["dtr"].mean(), 2) if len(ok) else None,
        }

    res = pd.concat(out)[["date", "stn", "station_name", "ta_avg", "ta_max", "ta_min", "hm_avg", "dtr", "frost", *EXTRA_COLUMNS]]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    res.to_csv(OUT, index=False, date_format="%Y-%m-%d", float_format="%.2f")
    rep_path = OUT.with_name("aws_weather_daily_report.json")
    rep_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"저장: {OUT} ({len(res)}행), {rep_path}")


if __name__ == "__main__":
    main()
