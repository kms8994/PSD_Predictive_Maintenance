"""기상청 API허브 ASOS 일자료(kma_sfcdd.php)를 날짜·관측소별로 받아 원본 CSV에 쌓는다.

이미 받은 (날짜, 관측소)는 건너뛰므로 중간에 끊겨도 다시 실행하면 이어서 받는다.

    python scripts/collect_weather.py --start 2016-01-01 --end 2025-12-31 --stations 152,159
"""
import argparse
import csv
import datetime as dt
import sys
import time
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


def load_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("KMA_AUTH_KEY="):
            return line.split("=", 1)[1].strip()
    sys.exit(".env에 KMA_AUTH_KEY가 없습니다.")


def fetch(session: requests.Session, key: str, day: dt.date, stn: str) -> list[list[str]]:
    params = {"tm": day.strftime("%Y%m%d"), "stn": stn, "help": "0", "authKey": key}
    for attempt in range(4):
        try:
            r = session.get(URL, params=params, timeout=60)
            if r.status_code == 200:
                text = r.content.decode("euc-kr", errors="replace")
                rows = [ln.rstrip(",").split(",") for ln in text.splitlines() if ln and not ln.startswith("#")]
                return [row[: len(COLUMNS)] for row in rows if len(row) >= 2 and row[1] == stn]
            if r.status_code in (401, 403):
                sys.exit(f"API 거부({r.status_code}): {r.text[:200]}")
        except requests.RequestException:
            pass
        time.sleep(2 ** attempt)
    raise RuntimeError(f"{day} {stn} 4번 실패")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--stations", default="152,159")
    ap.add_argument("--out", default=str(ROOT / "data/raw/asos_daily_raw.csv"))
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out.exists():
        with out.open(encoding="utf-8", newline="") as f:
            done = {(row["TM"], row["STN"]) for row in csv.DictReader(f)}

    start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
    days = [start + dt.timedelta(d) for d in range((end - start).days + 1)]
    stations = args.stations.split(",")
    todo = [(d, s) for d in days for s in stations if (d.strftime("%Y%m%d"), s) not in done]
    print(f"전체 {len(days) * len(stations)}건, 이미 받음 {len(done)}건, 남음 {len(todo)}건", flush=True)

    key = load_key()
    new_file = not out.exists()
    empty = []
    with requests.Session() as session, out.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(COLUMNS)
        for n, (day, stn) in enumerate(todo, 1):
            rows = fetch(session, key, day, stn)
            if not rows:
                empty.append(f"{day} {stn}")
            w.writerows(rows)
            if n % 200 == 0:
                f.flush()
                print(f"{n}/{len(todo)} ({day} {stn})", flush=True)
            time.sleep(0.05)
    print(f"완료. 응답이 비어 있던 날: {len(empty)}건", flush=True)
    if empty:
        print("\n".join(empty[:50]))


if __name__ == "__main__":
    main()
