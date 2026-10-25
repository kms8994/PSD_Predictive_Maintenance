"""고장(교체) 기록 더미데이터를 엑셀로 만든다 (2026-10-25 사용자 지시).

실제 고장 데이터가 없어서, 신뢰성 공식이 낸 권장 주기(base_years, DB의 cycle_results)를
평균으로 삼아 부품마다 "고장까지 걸린 기간"을 와이블 분포로 하나씩 뽑는다. 그 기간이 설치일
기준으로 오늘보다 이전이면(=이미 고장났을 법하면) 교체(고장) 기록 한 행을 만든다. 공식이
낸 숫자를 기준점으로 무작위성을 더한 것이지, 완전히 멋대로 만든 숫자는 아니다 — MVP_계획.md
"가상 정비 기록 생성" 항목과 같은 방식(신뢰성 공식 + 무작위성)이고, 나중에 ML 보정과 비교할
때 "정답"으로 쓸 수 있게 공식의 결과를 따라가게 만들었다.

와이블 분포(형상모수 2)를 쓰는 이유: 지수분포(형상모수 1)는 "오래된 부품일수록 고장 위험이
더 크다"는 마모 성질을 못 담아 초반 고장이 과하게 많이 나온다. 형상모수 2는 이 프로젝트가
나중에 쓰기로 한 Weibull 회귀(MVP_계획.md)와도 같은 가정이라 더 일관적이다.

psd/excel_io.py와 같은 열 형식으로 써서, 바로 POST /api/replacement-records/upload로
올릴 수 있다.

    python scripts/generate_dummy_records.py                    # 미리보기(저장 안 함)
    python scripts/generate_dummy_records.py --out FILE.xlsx    # 엑셀로 저장
    python scripts/generate_dummy_records.py --seed 1 --out ... # 재현 가능하게 시드 고정
"""
import argparse
import random
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from psd.catalog import NAME_TO_CODE  # noqa: E402
from psd.db import get_session  # noqa: E402
from psd.excel_io import REASON_CODE_TO_LABEL, rows_to_excel_bytes  # noqa: E402
from psd.models import CycleResult, Door, Part, PartType  # noqa: E402

CODE_TO_NAME = {code: name for name, code in NAME_TO_CODE.items()}
WEIBULL_SHAPE = 2.0  # 형상모수. 1이면 지수분포(마모 성질 없음), 클수록 "노후화 고장" 성질이 강해짐


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="저장할 .xlsx 경로. 안 주면 몇 건이 나오는지만 보여주고 끝")
    ap.add_argument("--seed", type=int, help="난수 시드(재현 가능하게)")
    ap.add_argument("--today", help="기준 날짜(YYYY-MM-DD). 안 주면 오늘")
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
    today = date.fromisoformat(args.today) if args.today else date.today()

    session = get_session()
    try:
        cycle_by_key = {
            (cr.station_id, cr.part_type_code): cr.base_years
            for cr in session.query(CycleResult).all()
        }
        doors_by_id = {d.id: d for d in session.query(Door).all()}
        part_types = {pt.code: pt for pt in session.query(PartType).all()}
        parts = session.query(Part).all()

        rows = []
        for part in parts:
            base_years = cycle_by_key.get((part.station_id, part.part_type_code), 10.0)
            years_to_fail = random.weibullvariate(base_years, WEIBULL_SHAPE)
            fail_date = part.install_date + timedelta(days=round(years_to_fail * 365.25))
            if fail_date > today:
                continue  # 아직 고장 안 났다고 본다 — 기록 안 만듦

            station = part.station  # relationship, 세션 살아있는 동안만 접근 가능
            pt = part_types[part.part_type_code]
            if pt.unit == "station":
                side_label, door_no = "", ""
            else:
                door = doors_by_id[part.door_id]
                from psd.catalog import SIDES
                side_label, door_no = SIDES[door.side], f"{door.car}-{door.position}"

            rows.append({
                "역": station.name,
                "방향": side_label,
                "문번호": door_no,
                "교체일": fail_date.isoformat(),
                "사유": REASON_CODE_TO_LABEL["fail"],
                "부품": CODE_TO_NAME[part.part_type_code],
                "입력자": "더미데이터",
            })

        print(f"기준 날짜 {today}, 전체 부품 {len(parts)}개 중 {len(rows)}건이 '이미 고장'으로 뽑힘")
        if not args.out:
            print("--out 없이 실행해서 저장은 안 함. 저장하려면 --out FILE.xlsx")
            return

        Path(args.out).write_bytes(rows_to_excel_bytes(rows))
        print(f"저장: {args.out}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
