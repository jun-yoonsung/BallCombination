"""볼배합 예측: 다음 공의 구종·코스 확률과 투구 가치(Run Value).

예)
  python -m kbo_crawler.predict "datasets/kbo_pitches/2026-*.csv" --report docs/prediction_report.md

모델은 일부러 단순한 계층형 빈도 모델이다. 같은 상황(투수 / +카운트 / +타자 타석 / +직전 구종)의
과거 투구 비율을 쓰되, 기록이 적으면 한 단계 넓은 상황의 비율 쪽으로 당긴다(디리클레 수축).
2026 시즌 9~10월을 검증 구간으로 비교했을 때 LightGBM과 정답률은 비슷하고(46.9% vs 47.4%)
확률의 정확도(로그 손실)는 더 좋았다(1.302 vs 1.316). 브라우저에서 그대로 계산할 수 있고
"비슷한 상황 몇 구를 근거로 했는지" 설명할 수 있다는 점 때문에 이 방식을 쓴다.

같은 계산을 web/predict.js가 브라우저에서 한다. 상수(알파 값, 존 경계)는 두 곳이 같아야 한다.

투구 가치: 2026 데이터로 아웃·주자 상황별 기대 득점(RE24)을 만들고, 타석 결과별 가치와
카운트별 가치를 구한 뒤, 공 하나가 바꾼 카운트(또는 타석 결과)의 가치 차이를 그 공의 가치로 본다.
양수면 타자에게 유리(실점 증가), 음수면 투수에게 유리하다.
"""

import argparse
import csv
import glob
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

# 구종 예측: 투수 / +카운트 / +타석 / +직전 구종 단계의 수축 강도, 타자 보정 강도
TYPE_ALPHAS = (10, 40, 80, 30)
BATTER_BETA = 100
# 코스 예측: 리그 (구종, 투수 손, 타석) -> +카운트 상황, 투수 (구종, 타석)
ZONE_ALPHA_LEAGUE = (5, 20)
ZONE_ALPHA_PITCHER = 80
# 투구 가치 수축 강도: 리그 (구종, 카운트) -> 투수 구종 -> +카운트 -> +타석
VALUE_ALPHA = 100

# 존: 리그 평균 높이(1.61~3.33ft)로 정규화, 좌우는 플레이트 반폭 + 공 반지름
ZONE_BOT, ZONE_TOP, ZONE_HALF_W = 1.61, 3.33, 0.83
N_ZONES = 13  # 0~8: 존 안 3x3 (위 왼쪽부터, 포수 시점), 9~12: 바깥 (위왼, 위오, 아래왼, 아래오)
SPLIT_DATE = "2026-09-01"


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def zone_of(x, z):
    """포수 시점 좌우 x(ft), 정규화된 높이 z(ft) -> 0~12. 값이 없으면 None."""
    if x is None or z is None:
        return None
    if abs(x) <= ZONE_HALF_W and ZONE_BOT <= z <= ZONE_TOP:
        col = min(2, int((x + ZONE_HALF_W) / (2 * ZONE_HALF_W) * 3))
        row = min(2, int((ZONE_TOP - z) / (ZONE_TOP - ZONE_BOT) * 3))
        return row * 3 + col
    low = z < (ZONE_BOT + ZONE_TOP) / 2
    return 9 + 2 * low + (x > 0)


def normalized_z(row):
    z, top, bot = _f(row.get("plate_z")), _f(row.get("sz_top")), _f(row.get("sz_bot"))
    if z is None or not top or not bot or top <= bot:
        return z
    return ZONE_BOT + (z - bot) / (top - bot) * (ZONE_TOP - ZONE_BOT)


def count_group(balls, strikes):
    if balls == 0 and strikes == 0:
        return "first"
    if strikes == 2:
        return "two"
    return "behind" if balls > strikes else "other"


def load_rows(paths):
    rows = []
    for path in paths:
        with open(path, encoding="utf-8-sig") as f:
            rows.extend(csv.DictReader(f))
    rows = [r for r in rows if r["pitch_type"]]
    rows.sort(key=lambda r: (r["game_date"], r["game_id"], _i(r["seqno"])))
    return rows


def annotate(rows):
    """타석 번호, 직전 구종, 카운트, 존, 투수 손을 행에 붙인다. rows는 경기·seqno 순이어야 한다."""
    pa, prev_key, prev_type = -1, None, None
    x0s = defaultdict(list)
    for r in rows:
        key = (r["game_id"], r["inning"], r["half"], r["batter_id"])
        if key != prev_key or _i(r["pitch_num"]) == 1:
            pa, prev_type = pa + 1, None
        r["_pa"], r["_prev"] = pa, prev_type or "-"
        r["_b"], r["_s"] = min(_i(r["balls"]), 3), min(_i(r["strikes"]), 2)
        r["_cnt"] = f"{r['_b']}{r['_s']}"
        r["_cg"] = count_group(r["_b"], r["_s"])
        r["_st"] = "L" if r["stance"] == "L" else "R"  # 타석 정보가 없는 공(0.04%)은 우타로 본다 (웹과 같게)
        r["_zone"] = zone_of(_f(r["plate_x"]), normalized_z(r))
        if _f(r.get("x0")) is not None:
            x0s[r["pitcher_id"]].append(float(r["x0"]))
        prev_key, prev_type = key, r["pitch_type"]
    hands = {p: ("L" if sorted(xs)[len(xs) // 2] > 0 else "R") for p, xs in x0s.items()}
    for r in rows:
        r["_hand"] = hands.get(r["pitcher_id"], "R")
    return rows


# ---------------------------------------------------------------- 투구 가치
def run_values(rows):
    """RE24, 타석 결과 가치, 카운트 가치를 만들고 각 행에 _rv(공 하나의 가치, 타자 기준 득점)를 붙인다."""
    bat = lambda r: _i(r["away_score"] if r["half"] == "top" else r["home_score"])
    half_end = defaultdict(int)
    for r in rows:
        h = (r["game_id"], r["inning"], r["half"])
        half_end[h] = max(half_end[h], bat(r))
    # 타석 단위로 묶기
    pas = []
    for r in rows:
        if not pas or pas[-1]["id"] != r["_pa"]:
            state = f"{min(_i(r['outs']), 2)}{_i(r['on_1b'])}{_i(r['on_2b'])}{_i(r['on_3b'])}"
            pas.append({"id": r["_pa"], "half": (r["game_id"], r["inning"], r["half"]), "state": state,
                        "score": bat(r), "rows": []})
        pas[-1]["rows"].append(r)
    for p in pas:
        p["res"] = p["rows"][-1].get("pa_result") or None
    re_sum, re_n = Counter(), Counter()
    for p in pas:
        re_sum[p["state"]] += half_end[p["half"]] - p["score"]
        re_n[p["state"]] += 1
    re24 = {s: re_sum[s] / re_n[s] for s in re_n}
    # 타석 가치 = 다음 타석 상황의 기대 득점 - 지금 기대 득점 + 이 타석에 난 점수
    for k, p in enumerate(pas):
        nxt = pas[k + 1] if k + 1 < len(pas) and pas[k + 1]["half"] == p["half"] else None
        end_re = re24[nxt["state"]] if nxt else 0.0
        end_score = nxt["score"] if nxt else half_end[p["half"]]
        p["value"] = end_re - re24[p["state"]] + (end_score - p["score"])
    w_sum, w_n = Counter(), Counter()
    c_sum, c_n = Counter(), Counter()
    for p in pas:
        if not p["res"]:
            continue
        w_sum[p["res"]] += p["value"]
        w_n[p["res"]] += 1
        for cnt in {r["_cnt"] for r in p["rows"]}:
            c_sum[cnt] += p["value"]
            c_n[cnt] += 1
    weights = {k: w_sum[k] / w_n[k] for k in w_n}
    count_value = {k: c_sum[k] / c_n[k] for k in c_n}
    for p in pas:
        rs = p["rows"]
        for k, r in enumerate(rs):
            if k + 1 < len(rs):
                r["_rv"] = count_value.get(rs[k + 1]["_cnt"], 0) - count_value.get(r["_cnt"], 0)
            elif p["res"] and _i(r.get("pa_last_pitch")):
                r["_rv"] = weights[p["res"]] - count_value.get(r["_cnt"], 0)
            else:
                r["_rv"] = None
    return {"re24": re24, "weights": weights, "count_value": count_value}


# ---------------------------------------------------------------- 구종 모델
class TypeModel:
    """계층형 빈도 모델. predict()는 구종 이름 -> 확률 dict를 돌려준다."""

    LEVELS = (("pitcher_id",), ("pitcher_id", "_cnt"), ("pitcher_id", "_cnt", "_st"), ("pitcher_id", "_cnt", "_st", "_prev"))

    def __init__(self, alphas=TYPE_ALPHAS, beta=BATTER_BETA):
        self.alphas, self.beta = alphas, beta

    def fit(self, rows):
        self.types = [t for t, _ in Counter(r["pitch_type"] for r in rows).most_common()]
        n = len(rows)
        lc = Counter(r["pitch_type"] for r in rows)
        self.league = {t: lc[t] / n for t in self.types}
        self.tables = [defaultdict(Counter) for _ in self.LEVELS]
        for r in rows:
            for tab, keys in zip(self.tables, self.LEVELS):
                tab[tuple(r[k] for k in keys)][r["pitch_type"]] += 1
        # 타자 보정: 타자가 실제로 본 구종 / 모델이 기대한 구종 비율을 beta만큼 1 쪽으로 당긴다
        obs, exp = defaultdict(Counter), defaultdict(Counter)
        for r in rows:
            p = self._base(r)
            for t in self.types:
                exp[r["batter_id"]][t] += p[t]
            obs[r["batter_id"]][r["pitch_type"]] += 1
        self.batter_ratio = {}
        for b, e in exp.items():
            tot = sum(e.values())
            self.batter_ratio[b] = {t: (obs[b][t] + self.beta * e[t] / tot) / (e[t] + self.beta * e[t] / tot) if e[t] else 1.0
                                    for t in self.types}
        return self

    def _base(self, r):
        p = dict(self.league)
        for tab, keys, a in zip(self.tables, self.LEVELS, self.alphas):
            c = tab.get(tuple(r[k] for k in keys))
            n = sum(c.values()) if c else 0
            p = {t: ((c[t] if c else 0) + a * p[t]) / (n + a) for t in self.types}
        return p

    def predict(self, r):
        p = self._base(r)
        ratio = self.batter_ratio.get(r.get("batter_id"))
        if ratio:
            p = {t: p[t] * ratio[t] for t in self.types}
            s = sum(p.values())
            p = {t: v / s for t, v in p.items()}
        return p


# ---------------------------------------------------------------- 코스 모델
class ZoneModel:
    """리그 (구종, 투수 손, 타석) -> +카운트 상황, 거기에 투수 개인 (구종, 타석) 경향을 곱한다."""

    def fit(self, rows):
        rows = [r for r in rows if r["_zone"] is not None]
        zc = Counter(r["_zone"] for r in rows)
        self.league = [zc[z] / len(rows) for z in range(N_ZONES)]
        self.l1, self.l2, self.pt = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
        for r in rows:
            self.l1[(r["pitch_type"], r["_hand"], r["_st"])][r["_zone"]] += 1
            self.l2[(r["pitch_type"], r["_hand"], r["_st"], r["_cg"])][r["_zone"]] += 1
            self.pt[(r["pitcher_id"], r["pitch_type"], r["_st"])][r["_zone"]] += 1
        return self

    @staticmethod
    def _smooth(c, prior, a):
        n = sum(c.values()) if c else 0
        return [((c[z] if c else 0) + a * prior[z]) / (n + a) for z in range(N_ZONES)]

    def predict(self, r, pitch_type, personal=True):
        a1, a2 = ZONE_ALPHA_LEAGUE
        l1 = self._smooth(self.l1.get((pitch_type, r["_hand"], r["_st"])), self.league, a1)
        l2 = self._smooth(self.l2.get((pitch_type, r["_hand"], r["_st"], r["_cg"])), l1, a2)
        if not personal:
            return l2
        pp = self._smooth(self.pt.get((r["pitcher_id"], pitch_type, r["_st"])), l1, ZONE_ALPHA_PITCHER)
        q = [pp[z] * l2[z] / l1[z] for z in range(N_ZONES)]
        s = sum(q)
        return [v / s for v in q]


# ---------------------------------------------------------------- 평가
def _logloss(probs, truth):
    return -sum(math.log(max(p, 1e-12)) for p, t in zip(probs, truth)) / len(truth) if truth else float("nan")


def evaluate(rows, split=SPLIT_DATE):
    train = [r for r in rows if r["game_date"] < split]
    test = [r for r in rows if r["game_date"] >= split]
    m = TypeModel().fit(train)
    # 기준: 그 투수가 시즌에 가장 많이 던진 구종 / 그 투수의 구종 비율
    per_p = defaultdict(Counter)
    for r in train:
        per_p[r["pitcher_id"]][r["pitch_type"]] += 1
    def base_dist(r):
        c = per_p.get(r["pitcher_id"])
        n = sum(c.values()) if c else 0
        return {t: ((c[t] if c else 0) + 10 * m.league[t]) / (n + 10) for t in m.types}
    preds, bases, truth_p, truth_b = [], [], [], []
    hit = hit2 = hit_b = 0
    by_pitcher = defaultdict(lambda: [0, 0, 0])
    for r in test:
        p, b = m.predict(r), base_dist(r)
        order = sorted(p, key=p.get, reverse=True)
        top_b = max(b, key=b.get)
        ok = order[0] == r["pitch_type"]
        hit += ok
        hit2 += r["pitch_type"] in order[:2]
        hit_b += top_b == r["pitch_type"]
        truth_p.append(p.get(r["pitch_type"], 1e-12))
        truth_b.append(b.get(r["pitch_type"], 1e-12))
        s = by_pitcher[r["pitcher_name"]]
        s[0] += 1; s[1] += ok; s[2] += top_b == r["pitch_type"]
    # 직구 vs 변화구로 묶었을 때
    fb = {"직구", "투심"}
    hit_fb = sum((sum(v for t, v in m.predict(r).items() if t in fb) >= 0.5) == (r["pitch_type"] in fb) for r in test)
    fb_share = sum(r["pitch_type"] in fb for r in test) / len(test)
    zm = ZoneModel().fit(train)
    zt = [r for r in test if r["_zone"] is not None]
    z_ll = _logloss([zm.predict(r, r["pitch_type"])[r["_zone"]] for r in zt], zt)
    z_lg = _logloss([zm.predict(r, r["pitch_type"], personal=False)[r["_zone"]] for r in zt], zt)
    hard = sorted(((k, v[1] / v[0], v[2] / v[0], v[0]) for k, v in by_pitcher.items() if v[0] >= 400), key=lambda x: x[1])
    return {
        "split": split, "n_train": len(train), "n_test": len(test), "types": m.types,
        "type": {"acc": hit / len(test), "top2": hit2 / len(test), "logloss": _logloss(truth_p, test),
                 "base_acc": hit_b / len(test), "base_logloss": _logloss(truth_b, test), "fb_acc": hit_fb / len(test),
                 "fb_base": max(fb_share, 1 - fb_share)},
        "zone": {"logloss": z_ll, "league_logloss": z_lg, "uniform_logloss": math.log(N_ZONES), "n": len(zt)},
        "hardest": [{"name": k, "acc": a, "base": b, "n": n} for k, a, b, n in hard[:5]],
        "easiest": [{"name": k, "acc": a, "base": b, "n": n} for k, a, b, n in hard[-5:][::-1]],
    }


def write_report(ev, rv, path):
    t, z = ev["type"], ev["zone"]
    pct = lambda v: f"{v * 100:.1f}%"
    lines = [
        "# 볼배합 예측 리포트 (2026 KBO)",
        "",
        f"학습: {SPLIT_DATE} 이전 {ev['n_train']:,}구 · 검증: 이후 {ev['n_test']:,}구 (9~10월, 학습에 쓰지 않은 경기)",
        "",
        "## 1. 다음 공 구종 예측",
        "",
        "| 방법 | 정답률 | 상위 2개 안 정답 | 로그 손실 |",
        "|---|---|---|---|",
        f"| 그 투수의 최다 구종 | {pct(t['base_acc'])} | – | {t['base_logloss']:.3f} |",
        f"| **계층형 빈도 모델** (투수·카운트·타석·직전 구종 + 타자 보정) | **{pct(t['acc'])}** | **{pct(t['top2'])}** | **{t['logloss']:.3f}** |",
        "",
        f"- 직구(직구·투심) vs 변화구 2지선다로 보면 정답률 {pct(t['fb_acc'])} (늘 많은 쪽을 찍으면 {pct(t['fb_base'])})",
        "- 같은 검증 구간에서 LightGBM(직전 공 위치·구속, 경기 내 투구 수 등 추가)은 정답률 47.4%, 로그 손실 1.316으로"
        " 정답률은 비슷하고 확률 정확도는 이 모델이 더 좋았습니다. 브라우저에서 바로 계산되고 근거를 설명할 수 있어 이 모델을 씁니다.",
        "- 투수가 일부러 섞어 던지기 때문에 상황 정보만으로 구종 하나를 맞히는 데에는 한계가 있습니다."
        " 한 구종을 단정하기보다 확률로 읽는 것이 맞습니다.",
        "",
        "### 읽기 어려운 투수 / 읽기 쉬운 투수 (검증 구간 400구 이상)",
        "",
        "| 투수 | 모델 정답률 | 최다 구종 찍기 | 검증 투구 수 |",
        "|---|---|---|---|",
    ]
    for x in ev["hardest"] + ev["easiest"]:
        lines.append(f"| {x['name']} | {pct(x['acc'])} | {pct(x['base'])} | {x['n']:,} |")
    lines += [
        "",
        "위 5명은 여러 구종을 고르게 섞어 예측이 가장 어려운 투수, 아래 5명은 패턴이 뚜렷한 투수입니다.",
        "",
        "## 2. 코스 예측 (존 9칸 + 바깥 4칸)",
        "",
        "| 방법 | 로그 손실 |",
        "|---|---|",
        f"| 13칸 균등 | {z['uniform_logloss']:.3f} |",
        f"| 리그 평균 (구종·투수 손·타석·카운트 상황) | {z['league_logloss']:.3f} |",
        f"| + 투수 개인 경향 | {z['logloss']:.3f} |",
        "",
        "구종을 알면 코스 분포는 꽤 좁혀지지만, 투수 개인 경향을 더해도 나아지는 폭은 작습니다."
        " 같은 투수도 공마다 코스가 크게 흩어지기 때문입니다. 코스 예측은 '이 구종이면 대체로 이쪽'을 보여주는 참고로 쓰세요.",
        "",
        "## 3. 투구 가치 (Run Value)",
        "",
        "2026 시즌 기록으로 만든 기대 득점표(RE24)에서 타석 결과별 가치를 구했습니다 (타자 기준 득점, 투수에게는 부호 반대).",
        "",
        "| 결과 | 가치 | 결과 | 가치 |",
        "|---|---|---|---|",
    ]
    w = sorted(rv["weights"].items(), key=lambda kv: kv[1])
    half = (len(w) + 1) // 2
    for (a, va), (b, vb) in zip(w[:half], w[half:] + [("", None)]):
        lines.append(f"| {a} | {va:+.3f} | {b} | {'' if vb is None else f'{vb:+.3f}'} |")
    re = rv["re24"]
    lines += [
        "",
        f"기대 득점 예: 무사 주자 없음 {re['0000']:.2f}점, 무사 만루 {re['0111']:.2f}점, 2사 주자 없음 {re['2000']:.2f}점",
        "",
        "공 하나의 가치는 그 공이 바꾼 카운트의 가치 차이(타석이 끝났으면 결과 가치와의 차이)입니다."
        " 탐색기의 투수 입장 화면은 이 값을 구종·카운트·타석별로 평균해 '100구당 막은 실점'으로 보여줍니다.",
        "",
        "## 4. 해석할 때 주의",
        "",
        "- 2026 한 시즌 데이터만 썼습니다. 시즌 중 구종을 바꾼 투수는 예측이 잘 맞지 않습니다.",
        "- 투수 입장의 '가치가 높은 구종'은 과거에 그 상황에서 결과가 좋았던 공이라는 뜻입니다."
        " 그 공을 더 자주 던지면 타자가 노리게 되므로, 그대로 따라 하라는 추천이 아니라 참고 자료입니다.",
        "- 구장마다 투구 추적 장비 보정 차이가 있어 코스 값에는 수 cm 오차가 있습니다.",
        "",
    ]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description="볼배합 예측 모델 평가와 리포트")
    ap.add_argument("csv", nargs="+")
    ap.add_argument("--report", default="docs/prediction_report.md")
    ap.add_argument("--json", help="평가 결과를 JSON으로도 저장")
    args = ap.parse_args(argv)
    paths = sorted({p for pattern in args.csv for p in glob.glob(pattern)})
    rows = annotate(load_rows(paths))
    rv = run_values(rows)
    ev = evaluate(rows)
    write_report(ev, rv, args.report)
    if args.json:
        Path(args.json).write_text(json.dumps(ev, ensure_ascii=False, indent=1), encoding="utf-8")
    t = ev["type"]
    print(f"구종 정답률 {t['acc']:.3f} (기준 {t['base_acc']:.3f}), 상위2 {t['top2']:.3f}, 로그손실 {t['logloss']:.3f}")
    print(f"코스 로그손실 {ev['zone']['logloss']:.3f} (리그 {ev['zone']['league_logloss']:.3f}) -> {args.report}")


if __name__ == "__main__":
    main()
