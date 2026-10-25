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

    # daily_weather.ta_avg/ta_max/hm_avg/dtr는 NOT NULL이다(ta_min만 결측 허용, psd/models.py
    # 주석 "남은 결측 1일(울산)" 참고). 코드 리뷰로 찾은 버그: 긴 결측 구간(예: 원본 수집
    # 시점 이후로 며칠 이상 비어 전처리의 3일 보간 한도를 넘는 경우)이 있으면 ta_avg 등이
    # NaN인 채로 insert를 시도해 IntegrityError로 전체가 실패했다. 그런 행은 애초에
    # daily_weather에 못 넣는 값이니 걸러내고, 몇 행을 걸렀는지는 알려준다.
    core = ["ta_avg", "ta_max", "hm_avg", "dtr"]
    before = len(out)
    out = out.dropna(subset=core)
    dropped = before - len(out)

    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM daily_weather")
        out.to_sql("daily_weather", conn, if_exists="append", index=False, chunksize=1000)
    msg = f"적재 완료: daily_weather {len(out)}행 ({out['date'].min().date()} ~ {out['date'].max().date()})"
    if dropped:
        msg += f" — 결측(3일 보간 한도 초과)으로 {dropped}행 제외"
    print(msg)


if __name__ == "__main__":
    main()
