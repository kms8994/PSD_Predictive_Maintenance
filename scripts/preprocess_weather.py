"""ASOS 일자료 원본에서 교체주기 계산에 쓸 항목만 뽑아 전처리한다.

- 기상청 결측 표시(-9, -99 등 음의 큰 값)는 빈값으로 바꾼다.
- 기온·습도의 3일 이하 짧은 공백만 앞뒤 값으로 선형 보간하고, 보간한 날은 표시한다.
- 일교차(최고 − 최저), 결빙일(최저기온 < 0°C)을 계산한다.

    python scripts/preprocess_weather.py
"""
import argparse
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
STATION_NAMES = {"152": "울산", "159": "부산"}  # 지점정보 API 미승인으로 이름은 추정
KEEP = {"TA_AVG": "ta_avg", "TA_MAX": "ta_max", "TA_MIN": "ta_min", "HM_AVG": "hm_avg", "RN_DAY": "rn_day"}
CORE = ["ta_avg", "ta_max", "ta_min", "hm_avg"]
MAX_GAP_DAYS = 3


def missing_to_nan(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce")
    return s.mask(s <= -9)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=str(ROOT / "data/raw/asos_daily_raw.csv"))
    ap.add_argument("--out", default=str(ROOT / "data/processed/weather_daily.csv"))
    args = ap.parse_args()

    raw = pd.read_csv(args.raw, dtype=str)
    df = raw[["TM", "STN", *KEEP]].rename(columns={"TM": "date", "STN": "stn", **KEEP})
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    df = df.drop_duplicates(["stn", "date"]).sort_values(["stn", "date"])
    for c in KEEP.values():
        df[c] = missing_to_nan(df[c])
    # 비가 안 온 날도 RN_DAY가 -9로 오므로 강수량 결측은 0mm로 본다(계산에는 쓰지 않는 참고값)
    df["rn_day"] = df["rn_day"].fillna(0.0)

    out, report = [], {}
    for stn, g in df.groupby("stn"):
        full = pd.date_range(g["date"].min(), g["date"].max(), freq="D")
        g = g.set_index("date").reindex(full)
        g.index.name = "date"
        g["stn"] = stn
        before = g[CORE].isna()
        for c in CORE:
            g[c] = g[c].interpolate(limit=MAX_GAP_DAYS, limit_area="inside")
        g["imputed"] = (before & g[CORE].notna()).any(axis=1)
        g["dtr"] = g["ta_max"] - g["ta_min"]
        g["frost"] = (g["ta_min"] < 0).astype("Int64").where(g["ta_min"].notna())
        g["station_name"] = STATION_NAMES.get(stn, "")
        out.append(g.reset_index())
        ok = g.dropna(subset=CORE)
        report[stn] = {
            "이름(추정)": STATION_NAMES.get(stn, ""),
            "기간": f"{full.min():%Y-%m-%d} ~ {full.max():%Y-%m-%d}",
            "전체 일수": len(g),
            "원본에 없던 날": int(len(full) - len(before.index.intersection(df[df.stn == stn]["date"]))),
            "결측(보간 전)": {c: int(before[c].sum()) for c in CORE},
            "보간한 날": int(g["imputed"].sum()),
            "남은 결측일": int(g[CORE].isna().any(axis=1).sum()),
            "연평균 기온": round(ok["ta_avg"].mean(), 2),
            "평균 습도": round(ok["hm_avg"].mean(), 2),
            "평균 일교차": round(ok["dtr"].mean(), 2),
            "연평균 결빙일": round(ok["frost"].astype(float).sum() / (len(ok) / 365.25), 1),
        }

    res = pd.concat(out)[["date", "stn", "station_name", *CORE, "dtr", "rn_day", "frost", "imputed"]]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    res.to_csv(args.out, index=False, date_format="%Y-%m-%d", float_format="%.1f")
    rep_path = Path(args.out).with_name("weather_daily_report.json")
    rep_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"저장: {args.out} ({len(res)}행), {rep_path}")


if __name__ == "__main__":
    main()
