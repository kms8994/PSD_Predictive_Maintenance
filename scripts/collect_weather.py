"""기상청 API허브 ASOS 일자료(kma_sfcdd.php)를 날짜별로 받아 원본 CSV에 쌓는다.

하루 1번 전 지점(stn=0)을 받아 대상 관측소만 남긴다. 대상 관측소가 모두 있는 날짜는 건너뛰므로
중간에 끊겨도 다시 실행하면 이어서 받는다.

    python scripts/collect_weather.py --start 2016-01-01 --end 2025-12-31 --stations 152,159
"""
import argparse
import csv
import datetime as dt
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
URL = "https://apihub.kma.go.kr/api/typ01/url/kma_sfcdd.php"
# kma_sfcdd.php help=1 응답의 필드 순서(56개)
COLUMNS = [
    "TM", "STN", "WS_AVG", "WR_DAY", "WD_MAX", "WS_MAX", "WS_MAX_TM", "WD_INS", "WS_INS", "WS_INS_TM",
    "TA_AVG", "TA_MAX", "TA_MAX_TM", "TA_MIN", "TA_MIN_TM", "TD_AVG", "TS_AVG", "TG_MIN", "HM_AVG", "HM_MIN",
    "HM_MIN_TM", "PV_AVG", "EV_S", "EV_L", "FG_DUR", "PA_AVG", "PS_AVG", "PS_MAX", "PS_MAX_TM", "PS_MIN",
    "PS_MIN_TM", "CA_TOT", "SS_DAY", "SS_DUR", "SS_CMB", "SI_DAY", "SI_60M_MAX", "SI_60M_MAX_TM", "RN_DAY", "RN_D99",
    "RN_DUR", "RN_60M_MAX", "RN_60M_MAX_TM", "RN_10M_MAX", "RN_10M_MAX_TM", "RN_POW_MAX", "RN_POW_MAX_TM", "SD_NEW", "SD_NEW_TM", "SD_MAX",
    "SD_MAX_TM", "TE_05", "TE_10", "TE_15", "TE_30", "TE_50",
]


class Denied(Exception):
    pass


def load_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("KMA_AUTH_KEY="):
            return line.split("=", 1)[1].strip()
    sys.exit(".env에 KMA_AUTH_KEY가 없습니다.")


def fetch_day(key: str, day: dt.date, stations: set[str]) -> list[list[str]]:
    params = {"tm": day.strftime("%Y%m%d"), "stn": "0", "help": "0", "authKey": key}
    for attempt in range(5):
        try:
            r = requests.get(URL, params=params, timeout=(5, 20))
            if r.status_code == 200:
                text = r.content.decode("euc-kr", errors="replace")
                rows = [ln.rstrip(",").split(",") for ln in text.splitlines() if ln and not ln.startswith("#")]
                return [row[: len(COLUMNS)] for row in rows if len(row) >= len(COLUMNS) and row[1] in stations]
            if r.status_code in (401, 403):
                raise Denied(f"API 거부({r.status_code}): {r.text[:200]}")
        except requests.RequestException:
            pass
        time.sleep(1 + 2 * attempt)
    raise RuntimeError(f"{day} 5번 실패")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--stations", default="152,159")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=str(ROOT / "data/raw/asos_daily_raw.csv"))
    args = ap.parse_args()

    stations = set(args.stations.split(","))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    have: dict[str, set[str]] = {}
    if out.exists():
        with out.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                have.setdefault(row["TM"], set()).add(row["STN"])

    start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
    days = [start + dt.timedelta(d) for d in range((end - start).days + 1)]
    todo = [d for d in days if not stations <= have.get(d.strftime("%Y%m%d"), set())]
    print(f"전체 {len(days)}일, 남음 {len(todo)}일", flush=True)

    key = load_key()
    new_file = not out.exists()
    missing, t0 = [], time.time()
    with out.open("a", encoding="utf-8", newline="") as f, ThreadPoolExecutor(args.workers) as pool:
        w = csv.writer(f)
        if new_file:
            w.writerow(COLUMNS)
        futures = {pool.submit(fetch_day, key, d, stations): d for d in todo}
        for n, fut in enumerate(as_completed(futures), 1):
            day = futures[fut]
            try:
                rows = fut.result()
            except Denied as e:
                pool.shutdown(cancel_futures=True)
                sys.exit(str(e))
            got = {r[1] for r in rows}
            already = have.get(day.strftime("%Y%m%d"), set())
            w.writerows(r for r in rows if r[1] not in already)
            f.flush()
            if stations - got - already:
                missing.append(f"{day} {sorted(stations - got - already)}")
            if n % 250 == 0:
                print(f"{n}/{len(todo)}일 ({time.time() - t0:.0f}초)", flush=True)
    print(f"완료 {len(todo)}일, {time.time() - t0:.0f}초. 관측값이 없던 날: {len(missing)}건", flush=True)
    if missing:
        print("\n".join(missing[:50]))


if __name__ == "__main__":
    main()
