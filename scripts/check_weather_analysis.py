"""역별·시기별 고장 분석(psd/analysis.py)이 더미의 숨은 정답을 찾아내는지 채점한다 (2026-11-27, 설계 1·2장).

정답(data/private/dummy/정답_수명모수.json, scripts/generate_dummy_records.py 날씨 정답 v4)과 비교:
1. 역 순위 — 정답의 역 × 소분류 진짜 평균 수명으로 만든 "진짜 역 고장 배수"와 분석의 역 고장 비율(순위 상관).
2. 날씨에 민감한 소분류 — 묶음마다 정답이 민감한 소분류 중 분석이 "잦음"으로 찾은 비율, 날씨 무관 소분류를 잘못 "잦음"으로
   찍은 비율(달 단위: 평년보다 그 묶음이 높았던 달 / 날 단위: 날씨 조건 뒤 일주일).
3. 짧은 충격 — 정답의 충격 소분류가 그 충격 조건 뒤 일주일에 "잦음"으로 나오는지.
4. 날씨 아닌 효과 — 센서·도어류의 주말 비율비, 해수욕장·관광지 역 여름이 "이 역만 여름에 유독"으로 잡히는지.

    python scripts/check_weather_analysis.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ANSWER = ROOT / "data" / "private" / "dummy" / "정답_수명모수.json"
SHOCK_TO_COND = {"heavy_rain": "heavy_rain", "storm": "wind", "thaw": "thaw", "heat": "heat"}


def spearman(a, b) -> float:
    from scipy.stats import spearmanr

    return float(spearmanr(a, b).statistic)


def main() -> None:
    from psd import analysis as A
    from psd.catalog import PART_TYPES
    from psd.db import get_session
    from psd.weather import BUNDLES

    truth = json.loads(ANSWER.read_text(encoding="utf-8"))
    units = truth["units"]
    session = get_session()
    try:
        panel = A.build_panel(session)
    finally:
        session.close()
    cm_all = np.ones(len(panel.codes), dtype=bool)
    dm = np.ones(len(panel.days), dtype=bool)

    # 1. 역 순위
    life = {(r["station"], r["part"]): r.get("true_station_mean_years", r["true_mean_years"]) for r in truth["station_part"]}
    E_sc = panel.E.sum(axis=2, dtype=np.float64)
    true_factor = []
    for i, sid in enumerate(panel.station_ids):
        name = panel.station_names[sid]
        num = den = 0.0
        for c, code in enumerate(panel.codes):
            lives = [life[(panel.station_names[s], code)] for s in panel.station_ids if (panel.station_names[s], code) in life]
            if (name, code) not in life or not lives:
                continue
            ratio = float(np.mean(lives)) / life[(name, code)]  # 수명이 노선 평균보다 짧으면 > 1
            num += E_sc[i, c] * ratio
            den += E_sc[i, c]
        true_factor.append(num / den if den else np.nan)
    rank = A.station_ranking(panel, cm_all, dm)
    got = {r["station_id"]: r["ratio"] for r in rank["stations"]}
    est = [got[sid] for sid in panel.station_ids]
    print(f"[1] 역 순위: 분석 고장 비율 vs 진짜 역 배수(수명 기준) 순위 상관 {spearman(est, true_factor):.2f} "
          "(충격·약한 문은 진짜 배수에 안 들어가 1이 될 수는 없다)")
    order = np.argsort(-np.array(true_factor))
    print("    진짜 상위 5: " + ", ".join(f"{panel.station_names[panel.station_ids[i]]} {true_factor[i]:.2f}" for i in order[:5]))
    print("    분석 상위 5: " + ", ".join(f"{r['station_name']} {r['ratio']:.2f}" for r in rank["stations"][:5]))
    print("    역 날씨로 설명: " + " · ".join(f"{e['label']} {e['rr']}({e['verdict']})" for e in rank["explain"] if e.get("rr")))

    # 2·3. 소분류별 날씨
    table = {r["code"]: r for r in A.part_weather_table(panel, dm)}
    print("\n[2] 날씨에 민감한 소분류 찾기 (고장 20건 이상인 소분류만)")
    none_codes = [c for c, u in units.items() if not u.get("aging") and not u.get("shock") and not u.get("weather")
                  and not table[c].get("too_few")]
    few = [c for c in units if table[c].get("too_few")]
    print(f"    고장이 20건 안 돼 재지 않은 소분류 {len(few)}종: {few}")
    for kind in ("daily", "monthly"):
        print(f"  ({'평년보다 높은 날, 같은 역·같은 달 안' if kind == 'daily' else '평년보다 높았던 달, 같은 역·같은 달 다른 해'})")
        for b, label in BUNDLES.items():
            sens = [c for c, u in units.items() if any(a["bundle"] == b for a in u.get("aging", [])) and not table[c].get("too_few")]
            hit = [f"{c} {table[c][kind][b]['rr']}" for c in sens if table[c][kind][b]["verdict"] == "잦음"]
            miss = [f"{c} {table[c][kind][b]['rr']}" for c in sens if table[c][kind][b]["verdict"] != "잦음"]
            false = [f"{c} {table[c][kind][b]['rr']}" for c in none_codes if table[c][kind][b]["verdict"] == "잦음"]
            print(f"    {label}: 정답 민감 {len(sens)}종 중 찾음 {hit} 못 찾음 {miss} · 날씨 무관 {len(none_codes)}종 중 잘못 찍음 {false}")

    print("\n[3] 짧은 충격")
    for c, u in units.items():
        sh = u.get("shock")
        if not sh or table[c].get("too_few"):
            continue
        cond = SHOCK_TO_COND[sh["event"]]
        r = table[c]["week"][cond]
        print(f"    {c} {sh['event']}(뒤 {sh['lag']}일): 조건 뒤 일주일 비율비 {r['rr']} ({r['lo']}~{r['hi']}) {r['verdict']}")

    # 4. 날씨 아닌 효과
    by = {p["code"]: p for p in PART_TYPES}
    usage = np.array([units[c].get("usage", False) for c in panel.codes])
    w_use = A.when(panel, usage, dm)
    w_oth = A.when(panel, ~usage & np.array([by[c]["unit"] == "door" for c in panel.codes]), dm)
    print(f"\n[4] 주말: 센서·도어류(정답 1.3배, 평균 1) {w_use['calendar'][0]['rr']} ({w_use['calendar'][0]['lo']}~{w_use['calendar'][0]['hi']}) · "
          f"그 밖 문 장비 {w_oth['calendar'][0]['rr']} ({w_oth['calendar'][0]['lo']}~{w_oth['calendar'][0]['hi']})")
    beach = truth["weather_truth"]["beach_stations"]
    summer = [s for s in w_use["station_season"] if s["season"] == "여름"]
    found = [s["station_name"] for s in summer if s["station_name"] in beach]
    others = [s["station_name"] for s in summer if s["station_name"] not in beach]
    print(f"    여름에 유독(센서·도어류): 정답 {beach} 중 찾음 {found} · 그 밖에 잡힌 역 {others}")


if __name__ == "__main__":
    main()
