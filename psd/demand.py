"""내년 부품 사용량(예산) 예측 — 장애부위 소분류 × 업체 (2026-11-28, 미팅/코레일_피드백_정리_1127.md 1장).

코레일 담당자 요구: 연간 계획 예산을 잡으려면 내년에 부품을 몇 개 쓸지 알아야 한다. 올해 쓴 만큼이 아니라 모델로,
전체와 업체별(일광까지 업체 1, 좌천~태화강 업체 2)로.

방법 — 부품 하나하나를 지금 나이에서 출발시켜 내년 말까지 굴려 본다(몬테카를로, N_SIMS번):

1. 수명: 기록으로 보정한 와이블(psd/calibrate.py, 역 × 소분류마다 평균 수명·형상 β·80% 범위). 평균 수명은 그 역의 평균
   날씨로 이미 달력 시간에 맞춰져 있다(내년 날씨 = 평년).
2. 지금 나이: 기록에서 이어 온 가상 나이(교체 = 0, 수리·조정 = 그대로, 단순교체 = 절반, psd/calibrate.py AGE_FACTOR).
   그 나이까지 버텼다는 조건을 걸고 다음 고장까지 기간을 뽑는다(조건부 와이블).
3. 고장 나면 조치를 그 소분류의 지난 사후조치 비율대로 뽑고(교체·단순교체·수리 ...), 나이를 조치대로 넘긴 뒤 다음 고장을 뽑는다.
   한 부품이 한 해에 두 번 고장 날 수도 있다 — 교체 예정일 하나만 세는 교체 계획(api.replacement_plan)과 다른 점.
4. 수명 불확실성: 매 회 역 × 소분류마다 평균 수명을 80% 범위에 맞춘 로그정규로 흔든다(같은 역·소분류의 부품은 함께 흔들린다).
5. 사전조치(예방) 교체: 고장과 따로 사람이 정하는 일이라 지난 1년 실적의 비율로 더한다(포아송).

출발점은 **마지막 기록 날**이다. 기록이 7월까지만 오면 8월~연말도 모델이 채운다 — 그래서 "올해 예상"이 따로 나온다.
부품 사용량 = 조치가 교체(주요 부품) 또는 단순교체(부속 부품)인 건수. 수리·조정·리셋·기타는 부품을 사지 않는 것으로 본다.

한계(화면에도 적는다): 사전조치가 나이를 되돌리는 효과는 시뮬레이션에 넣지 않아 고장 쪽이 조금 많게 나올 수 있다.
더미 기록으로 나온 숫자는 "시뮬레이터 가정이 맞다면"이다.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from psd.calibrate import AGE_FACTOR, PSD_START, part_intervals

N_SIMS = 500
SEED = 20261128
Z80 = 1.2815515655446004  # 표준정규 90% 분위 — 80% 범위의 반폭
USES = ("full", "partial")  # 부품을 쓰는 조치: 교체(주요 부품), 단순교체(부속 부품)
MAX_EVENTS = 40  # 한 부품이 남은 기간에 겪는 고장 수 상한(무한 루프 방지 — 평균 수명이 아주 짧게 잡혀도)


@dataclass
class SimResult:
    """counts[s, w, g, a]: s번째 시뮬레이션, 기간 w(0 = 출발~올해 말, 1 = 내년), 묶음 g, 조치 a의 고장 건수."""
    counts: np.ndarray
    unit_mean: np.ndarray  # [u, w, a] 부품마다 평균 건수(역별 표용)


def simulate(age: np.ndarray, eta: np.ndarray, beta: np.ndarray, sigma: np.ndarray, cell: np.ndarray,
             group: np.ndarray, n_groups: int, mix: np.ndarray, factor: np.ndarray, t0: float, t1: float,
             n_sims: int = N_SIMS, seed: int = SEED) -> SimResult:
    """부품 U개를 [0, t1]년 동안 굴린다. 기간 0 = [0, t0), 기간 1 = [t0, t1].

    age, eta(와이블 척도, 년), beta: 부품마다. sigma: 부품마다 척도 흔들림(로그 표준편차), cell: 함께 흔들릴 묶음 번호(역 × 소분류).
    group: 부품마다 결과 묶음 번호(소분류 × 업체 ...). mix[u, a]: 고장 나면 조치 a일 확률(행 합 1). factor[a]: 조치 뒤 남는 나이 비율.
    """
    rng = np.random.default_rng(seed)
    U, A = len(age), mix.shape[1]
    counts = np.zeros((n_sims, 2, n_groups, A))
    unit_sum = np.zeros((U, 2, A))
    if U == 0:
        return SimResult(counts, unit_sum)
    n_cells = int(cell.max()) + 1
    cum_mix = np.cumsum(mix, axis=1)
    cum_mix[:, -1] = 1.0
    for s in range(n_sims):
        z = rng.standard_normal(n_cells)
        # 보정 주기(calibrate의 years)는 log 평균의 exp(가운데 값)이라 그 둘레로 흔든다. 수명이 짧게 뽑힌 회의 고장이 길게
        # 뽑힌 회보다 더 많이 늘어(건수 ∝ 1/수명) 평균 건수는 불확실성만큼 조금 올라간다 — 예산은 이 몫까지 잡는 게 맞다
        scale = eta * np.exp(sigma * z[cell])
        a = age.astype(float).copy()
        t = np.zeros(U)
        alive = np.ones(U, dtype=bool)
        for _ in range(MAX_EVENTS):
            idx = np.flatnonzero(alive)
            if not len(idx):
                break
            # 조건부 와이블: 나이 a까지 버텼을 때 다음 고장까지 dt. S(a+dt)/S(a) = u
            b, sc, ai = beta[idx], scale[idx], a[idx]
            e = -np.log(rng.random(len(idx)))
            dt = sc * ((ai / sc) ** b + e) ** (1 / b) - ai
            t[idx] += dt
            done = t[idx] > t1
            alive[idx[done]] = False
            hit = idx[~done]
            if not len(hit):
                continue
            act = (rng.random(len(hit))[:, None] > cum_mix[hit]).sum(axis=1)  # 누적 확률로 조치 고르기
            w = (t[hit] >= t0).astype(int)
            np.add.at(counts[s], (w, group[hit], act), 1)
            np.add.at(unit_sum, (hit, w, act), 1)
            a[hit] = (a[hit] + dt[~done]) * factor[act]
    return SimResult(counts, unit_sum / n_sims)


def summarize(x: np.ndarray) -> dict:
    """시뮬레이션 건수 → 평균과 80% 범위(정수로 반올림)."""
    x = np.asarray(x, dtype=float)
    lo, hi = np.percentile(x, [10, 90]) if len(x) else (0.0, 0.0)
    return {"mean": round(float(x.mean()), 1) if len(x) else 0.0, "lo": int(math.floor(lo)), "hi": int(math.ceil(hi))}


# ---------- DB에서 읽어 예측 ----------


def forecast(session, year: int | None = None, today: date | None = None, n_sims: int = N_SIMS) -> dict:
    """내년(year) 부품 사용량을 소분류 × 업체로. 화면·엑셀이 같은 결과를 쓴다."""
    from sqlalchemy import select

    from psd.calibrate import calibration_map
    from psd.catalog import ACTIONS, PART_TYPES
    from psd.models import Part, ReplacementPart, ReplacementRecord
    from psd.stations import STATION_BY_ID, VENDORS

    today = today or date.today()
    year = year or today.year + 1
    actions = list(ACTIONS)
    a_index = {k: i for i, k in enumerate(actions)}
    factor = np.array([AGE_FACTOR.get(k, 0.0) for k in actions])

    rows = session.execute(
        select(ReplacementRecord.station_id, ReplacementRecord.door_id, ReplacementPart.part_type_code, ReplacementRecord.replaced_on,
               ReplacementRecord.reason, ReplacementRecord.action)
        .join(ReplacementPart, ReplacementPart.record_id == ReplacementRecord.id)
    ).all()
    data_end = max((r.replaced_on for r in rows), default=today)
    data_end = min(data_end, today)
    start = data_end  # 시뮬레이션 출발점: 마지막 기록 날
    y0, y1 = date(year, 1, 1), date(year, 12, 31)
    if start >= y0:
        raise ValueError(f"{year}년은 이미 기록이 있는 해입니다 — 내년 이후 해만 예측합니다")
    t0 = (y0 - start).days / 365.25
    t1 = ((y1 - start).days + 1) / 365.25

    codes = [p["code"] for p in PART_TYPES]
    by_part: dict[tuple, list] = {}
    fail_mix: dict[str, Counter] = {c: Counter() for c in codes}
    prev_rate: dict[tuple[int, str], Counter] = {}  # (역, 소분류) → 지난 1년 사전조치 교체·단순교체 수
    actual: dict[int, dict[tuple[str, str], Counter]] = {}  # 연도 → (소분류, 업체) → 조치별 실제 건수
    year_ago = data_end - timedelta(days=365)
    for sid, door_id, code, d, reason, action in rows:
        by_part.setdefault((sid, door_id, code), []).append((d, reason, action))
        if code not in fail_mix:
            continue
        if reason == "fail":
            fail_mix[code][action] += 1
        elif action in USES and d > year_ago:
            prev_rate.setdefault((sid, code), Counter())[action] += 1
        vendor = STATION_BY_ID[sid].vendor if sid in STATION_BY_ID else ""
        actual.setdefault(d.year, {}).setdefault((code, vendor), Counter())[action] += 1
    overall = sum(fail_mix.values(), Counter()) or Counter({"full": 1})

    cal = calibration_map(session, today)
    vendors = [*VENDORS, ""]
    g_index = {(c, v): i for i, (c, v) in enumerate((c, v) for c in codes for v in vendors)}
    cell_index: dict[tuple[int, str], int] = {}
    U = []
    for part_id, sid, door_id, code in session.execute(
            select(Part.id, Part.station_id, Part.door_id, Part.part_type_code).order_by(Part.id)):
        c = cal.get((sid, code))
        if c is None or code not in fail_mix:
            continue
        iv = part_intervals(PSD_START[sid], by_part.get((sid, door_id, code), []), start)
        age = iv[-1][0] + iv[-1][1] if iv else max((start - PSD_START[sid]).days / 365.25, 0.0)
        beta = float(c.shape)
        mean_years = float(c.raw_years) if math.isfinite(c.raw_years) and c.raw_years > 0 else float(c.years)
        eta = mean_years / math.gamma(1 + 1 / beta)
        sigma = (math.log(c.hi_years / c.lo_years) / (2 * Z80)
                 if math.isfinite(c.lo_years) and math.isfinite(c.hi_years) and c.lo_years > 0 and c.hi_years > c.lo_years else 0.0)
        vendor = STATION_BY_ID[sid].vendor if sid in STATION_BY_ID else ""
        U.append((part_id, sid, code, age, eta, beta, sigma, cell_index.setdefault((sid, code), len(cell_index)), g_index[(code, vendor)]))

    mix = np.array([[(fail_mix[u[2]] or overall)[k] for k in actions] for u in U], dtype=float).reshape(len(U), len(actions))
    mix = mix / np.maximum(mix.sum(axis=1, keepdims=True), 1e-12)
    arr = lambda i, t=float: np.array([u[i] for u in U], dtype=t)  # noqa: E731
    res = simulate(arr(3), arr(4), arr(5), arr(6), arr(7, int), arr(8, int), len(g_index), mix, factor, t0, t1, n_sims=n_sims)

    # 사전조치 교체: 지난 1년 비율 × 기간 길이(년), 포아송으로 흔든다
    rng = np.random.default_rng(SEED + 1)
    prev = np.zeros_like(res.counts)
    for (sid, code), cnt in prev_rate.items():
        vendor = STATION_BY_ID[sid].vendor if sid in STATION_BY_ID else ""
        g = g_index[(code, vendor)]
        for k, n in cnt.items():
            prev[:, 0, g, a_index[k]] += rng.poisson(n * t0, n_sims)
            prev[:, 1, g, a_index[k]] += rng.poisson(n * (t1 - t0), n_sims)
    total = res.counts + prev
    use = [a_index[k] for k in USES]

    def block(sel_groups: list[int]) -> dict:
        """묶음들을 더한 시뮬레이션 → 조치별 요약. 내년(next), 올해 남은 기간(rest)."""
        out = {}
        for key, src in (("next", total[:, 1]), ("rest", total[:, 0]), ("next_fail", res.counts[:, 1])):
            sub = src[:, sel_groups, :].sum(axis=1)
            out[key] = {k: summarize(sub[:, a_index[k]]) for k in USES}
            out[key]["uses"] = summarize(sub[:, use].sum(axis=1))
            if key == "next_fail":
                out[key]["failures"] = summarize(sub.sum(axis=1))
        return out

    def actual_of(y: int, sel: list[tuple[str, str]]) -> dict:
        c = sum((actual.get(y, {}).get(k, Counter()) for k in sel), Counter())
        return {k: c[k] for k in USES} | {"uses": sum(c[k] for k in USES)}

    scopes = {"전체": vendors, **{v: [v] for v in VENDORS}}
    names = {p["code"]: p for p in PART_TYPES}
    out_rows = []
    for code in codes:
        row = {"code": code, "name": names[code]["name"], "group": names[code]["group"], "unit": names[code]["unit"], "by": {}}
        for scope, vs in scopes.items():
            sel = [(code, v) for v in vs]
            b = block([g_index[k] for k in sel])
            this_rec = actual_of(today.year, sel)
            b["last_year"] = actual_of(year - 2, sel) if year - 2 >= min(PSD_START.values()).year else None
            b["this_year_recorded"] = this_rec
            b["this_year_expected"] = {k: round(this_rec[k] + b["rest"][k]["mean"], 1) for k in (*USES, "uses")} if year == today.year + 1 else None
            row["by"][scope] = b
        out_rows.append(row)
    totals = {}
    for scope, vs in scopes.items():
        sel = [(c, v) for c in codes for v in vs]
        b = block([g_index[k] for k in sel])
        this_rec = actual_of(today.year, sel)
        b["last_year"] = actual_of(year - 2, sel) if year - 2 >= min(PSD_START.values()).year else None
        b["this_year_recorded"] = this_rec
        b["this_year_expected"] = {k: round(this_rec[k] + b["rest"][k]["mean"], 1) for k in (*USES, "uses")} if year == today.year + 1 else None
        totals[scope] = b

    # 역 × 소분류 평균(엑셀 '역별' 시트) — 부품 평균을 더한 값
    station_rows: dict[tuple[int, str], np.ndarray] = {}
    for u, row in zip(U, res.unit_mean):
        station_rows[(u[1], u[2])] = station_rows.get((u[1], u[2]), 0) + row[1]
    for (sid, code), cnt in prev_rate.items():
        extra = np.zeros(len(actions))
        for k, n in cnt.items():
            extra[a_index[k]] = n * (t1 - t0)
        station_rows[(sid, code)] = station_rows.get((sid, code), 0) + extra
    by_station = [{"station_id": sid, "station": STATION_BY_ID[sid].name if sid in STATION_BY_ID else str(sid),
                   "vendor": STATION_BY_ID[sid].vendor if sid in STATION_BY_ID else "", "code": code, "name": names[code]["name"],
                   **{k: round(float(v[a_index[k]]), 2) for k in USES}}
                  for (sid, code), v in sorted(station_rows.items(), key=lambda kv: (STATION_BY_ID[kv[0][0]].seq if kv[0][0] in STATION_BY_ID else 999,
                                                                                      codes.index(kv[0][1])))]

    return {
        "year": year, "today": today, "data_end": data_end, "data_gap_days": (today - data_end).days,
        "n_sims": n_sims, "units": len(U), "uses": {k: ACTIONS[k] for k in USES}, "vendors": list(VENDORS),
        "no_vendor_stations": sorted({STATION_BY_ID[u[1]].name for u in U if u[1] in STATION_BY_ID and not STATION_BY_ID[u[1]].vendor}),
        "rows": out_rows, "totals": totals, "by_station": by_station,
    }
