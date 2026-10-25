"""ASOS 어제 날씨를 하루치만 받아 원본 CSV에 이어 붙이고 전처리까지 다시 돌린다.

2026-10-25 사용자 지시("날씨는 매일 API로 업데이트") 반영. 자세한 내용은
설계_v2.md 2-6·4-5 "날씨 자동화" 항목 참고 — 요약하면 두 가지를 구분해야 한다:

1. **"다음 교체 예정일까지 남은 기간"(빨강·노랑·초록)은 이미 매일 자동으로 바뀐다.**
   psd/status.py의 to_unit_status()가 date.today()를 그때그때 받아 계산하기 때문에,
   API를 호출할 때마다(= 매일 화면을 열 때마다) 저절로 최신 날짜 기준으로 평가된다.
   이 스크립트나 어떤 스케줄러도 필요 없다.
2. **권장 주기(base_years) 자체는 "기준 기후"(여러 해 평균)로 정해서, 하루 새 날씨가
   더 들어온다고 눈에 띄게 바뀌지 않는다.** 그래서 이 스크립트를 "매일" 돌려도 매번
   compute_cycles.py까지 다시 돌릴 필요는 없다 — 원본 날씨 보관소를 밀리지 않게
   쌓아 두는 용도고, 실제 재계산(아래 --recompute)은 한 달에 한 번 정도면 충분하다.

AWS(방재기상관측)는 API허브 미승인(403)이라 이 스크립트로 자동화할 수 없다 — 기상자료
개방포털 웹에서 사람이 직접 로그인해 받아야 한다(설계_v2.md 할 일). 그래서 이 스크립트는
ASOS만 다룬다. 지금 실제 계산(psd/cycle.py compute_all)은 AWS 기준이라, 이 스크립트가
갱신하는 ASOS 자료는 scripts/load_weather.py의 DB 적재(화면에서 안 읽음)에만 쓰인다 —
AWS 쪽 자동화 수단이 생기기 전까지는 "매일 자동 업데이트"의 전 구간을 다 채우지 못한다는
한계를 분명히 밝혀 둔다.

    python scripts/daily_weather_update.py                # 어제치만 받음
    python scripts/daily_weather_update.py --recompute     # 받고 나서 DB까지 재계산
    python scripts/daily_weather_update.py --date 2026-10-24  # 특정 날짜(재시도용)

Windows 작업 스케줄러에 매일 새벽 등록해 두면 된다(예: 매일 06:00, 이 명령 그대로).
"""
import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.collect_weather import COLUMNS, Denied, fetch_day, load_key  # noqa: E402

RAW_CSV = ROOT / "data/raw/asos_daily_raw.csv"
STATIONS = {"152", "159"}


def already_have(day: dt.date) -> set[str]:
    if not RAW_CSV.exists():
        return set()
    import csv

    want = day.strftime("%Y%m%d")
    have = set()
    with RAW_CSV.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["TM"] == want:
                have.add(row["STN"])
    return have


def append_day(day: dt.date, rows: list[list[str]]) -> None:
    import csv

    new_file = not RAW_CSV.exists()
    with RAW_CSV.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(COLUMNS)
        w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="받을 날짜(YYYY-MM-DD). 안 주면 어제")
    ap.add_argument("--recompute", action="store_true", help="받은 뒤 전처리 + DB 계산 결과까지 다시 돌림")
    args = ap.parse_args()

    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today() - dt.timedelta(days=1)

    have = already_have(day)
    missing = STATIONS - have
    if not missing:
        print(f"{day}: 이미 다 있음(부산·울산), 받을 거 없음")
    else:
        key = load_key()
        try:
            rows = fetch_day(key, day, missing)
        except Denied as e:
            sys.exit(f"API 거부: {e}")
        got = {r[1] for r in rows}
        append_day(day, rows)
        print(f"{day}: {len(rows)}행 받음({sorted(got)}). 빠진 지점: {sorted(missing - got) or '없음'}")

    if args.recompute:
        print("전처리·DB 재계산 시작...")
        subprocess.run([sys.executable, str(ROOT / "scripts/preprocess_weather.py")], check=True)
        subprocess.run([sys.executable, str(ROOT / "scripts/load_weather.py")], check=True)
        print("ASOS는 여기까지 — 실제 예측(compute_cycles.py)은 AWS 기준이라 이 스크립트로는 안 바뀜.")
    else:
        print("원본만 받음. 전처리·재계산까지 하려면 --recompute")


if __name__ == "__main__":
    main()
