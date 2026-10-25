"""AWS(방재기상관측) 시간 자료에서 일별 값을 만든다 (설계_v2.md 2-6 "동 단위" 세분화).

기상자료개방포털(data.kma.go.kr) 파일셋에서 받은 시간 자료(`data/raw/aws/hourly/*.csv`,
`SURFACE_AWS_<지점>_HR_<연도>_<연도>_<생산연도>.csv`, euc-kr)를 지점·날짜별로 모아 하루
평균·최고·최저 기온과 평균 습도를 낸다. ASOS 일자료(`preprocess_weather.py`)와 같은 열 이름을
쓴다 — psd/cycle.py의 AF 계산이 그대로 재사용되게 하기 위해서다.

AWS 시간 자료는 결측이 빈 문자열로 온다(예: 레이더 전용국은 기압이 항상 빈값). 하루 안에
기온·습도 관측이 하나도 없는 날만 결측으로 남기고, 나머지는 있는 값만으로 평균 낸다.

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
    for c in ("ta", "hm"):
        raw[c] = pd.to_numeric(raw[c], errors="coerce")
    return raw.drop_duplicates(["stn", "datetime"])


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
        full = pd.date_range(daily.index.min(), daily.index.max(), freq="D")
        daily = daily.reindex(full)
        daily.index.name = "date"
        # 그날 관측이 하나도 없으면(ta_n/hm_n이 0 또는 NaN) 결측으로 남긴다
        daily.loc[daily["ta_n"].fillna(0) == 0, ["ta_avg", "ta_max", "ta_min"]] = pd.NA
        daily.loc[daily["hm_n"].fillna(0) == 0, "hm_avg"] = pd.NA
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

    res = pd.concat(out)[["date", "stn", "station_name", "ta_avg", "ta_max", "ta_min", "hm_avg", "dtr", "frost"]]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    res.to_csv(OUT, index=False, date_format="%Y-%m-%d", float_format="%.2f")
    rep_path = OUT.with_name("aws_weather_daily_report.json")
    rep_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"저장: {OUT} ({len(res)}행), {rep_path}")


if __name__ == "__main__":
    main()
