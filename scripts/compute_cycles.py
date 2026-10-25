"""`psd/cycle.py`로 계산한 권장 주기를 DB에 저장한다 (MVP_계획.md 3-6).

날씨가 갱신되면 다시 실행해서 덮어쓴다.

    python scripts/compute_cycles.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from psd.catalog import NAME_TO_CODE  # noqa: E402
from psd.cycle import STATIONS, compute_all, load_aws_weather  # noqa: E402
from psd.db import get_session  # noqa: E402
from psd.models import CycleResult  # noqa: E402

NAME_TO_STATION_ID = {s.name: s.order for s in STATIONS}


def main() -> None:
    weather = load_aws_weather()
    start, end = weather["date"].min().date(), weather["date"].max().date()

    session = get_session()
    try:
        results = compute_all()
        for r in results:
            session.merge(
                CycleResult(
                    station_id=NAME_TO_STATION_ID[r.station],
                    part_type_code=NAME_TO_CODE[r.part],
                    base_years=r.cycle.base_years,
                    lo_years=r.cycle.lo_years,
                    hi_years=r.cycle.hi_years,
                    judgment=r.cycle.judgment,
                    weather_start=start,
                    weather_end=end,
                )
            )
        session.commit()
        print(f"계산 결과 저장: {len(results)}건 (날씨 {start} ~ {end})")
    finally:
        session.close()


if __name__ == "__main__":
    main()
