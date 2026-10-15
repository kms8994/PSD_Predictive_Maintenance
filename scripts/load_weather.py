"""전처리한 날씨를 DB에 적재한다 (MVP_계획.md 3-5).

    python scripts/load_weather.py
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from psd.cycle import WEATHER_CSV  # noqa: E402
from psd.db import engine  # noqa: E402


def main() -> None:
    df = pd.read_csv(WEATHER_CSV, parse_dates=["date"])
    df["stn"] = df["stn"].astype(str)
    out = df.rename(columns={"stn": "observatory_id"})[
        ["observatory_id", "date", "ta_avg", "ta_max", "ta_min", "hm_avg", "dtr", "frost", "imputed"]
    ]
    out["frost"] = out["frost"].astype("boolean")
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM daily_weather")
        out.to_sql("daily_weather", conn, if_exists="append", index=False, chunksize=1000)
    print(f"적재 완료: daily_weather {len(out)}행 ({out['date'].min().date()} ~ {out['date'].max().date()})")


if __name__ == "__main__":
    main()
