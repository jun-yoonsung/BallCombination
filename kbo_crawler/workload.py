"""투수 피로도·등판 가능 여부·엔트리 말소(부상 의심) 위험과 예상 선발.

예)
  python -m kbo_crawler.workload datasets/kbo_games/pitching.csv --report docs/workload_report.md --web web/workload.json

부상 기록은 공개 데이터에 없다. 그래서 두 가지를 "실제로 일어난 일"로 삼아 배운다.

1. 등판 가능성 (불펜): 그날 1군 엔트리에 있던 구원 투수가 실제로 나왔는가.
   KBO 감독·코치진은 연투, 전날 투구 수, 최근 1주 부하를 보고 투수를 쉬게 한다. 그 판단을 로지스틱 회귀로
   흉내 내고, 같은 투수가 푹 쉬었을 때 대비 등판 확률이 얼마나 줄었는지(가용도)로 피로도를 매긴다.
   피로도 = 100 x (1 - 가용도). 엔트리(boxscore.py)를 쓰므로 2군에 내려가 있던 날은 "안 쓴 날"로 세지 않는다.
2. 엔트리 말소 위험: 등판 뒤 팀의 다음 3경기 안에 1군 엔트리에서 빠졌는가.
   말소에는 부상 말고 부진에 따른 2군행도 섞여 있다. 직전 등판의 주 직구 구속이 최근 평균보다 떨어진 정도와
   그 경기 실점을 함께 넣어, 구속 저하 쪽 위험을 "부상 의심 신호"로 따로 보여 준다.

선발 투수는 로테이션으로 움직이므로 회귀 대신 "마지막 선발 후 며칠째인가"별 실제 선발 비율을 쓴다.
예상 선발은 팀의 최근 선발 투수 가운데 그 비율이 가장 높은(오래 쉰) 투수다.

판정 기준(가능/주의/휴식 권장)은 이 모델의 숫자에 붙인 경계값이고, 의학적 판단이 아니다.
"""

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

SPLIT_DATE = "2026-09-01"
SHRINK = 20  # 투수별 평소 등판 비율을 리그 평균 쪽으로 당기는 강도 (가상 엔트리 일수)
L2 = 1.0
MIN_ROSTER = 10  # 엔트리가 이보다 적게 잡힌 경기는 원본 누락으로 보고 학습에서 뺀다
DROP_WINDOW = 3  # 말소 위험: 등판 뒤 팀의 다음 3경기
VERDICTS = ("가능", "주의", "휴식 권장")
# 불펜 판정: 가용도(푹 쉬었을 때 대비 등판 확률) 경계
RATIO_REST, RATIO_CAUTION = 0.3, 0.65
# 말소 위험이 평소의 이 배수 이상이고 구속이 떨어졌으면 '주의'로 올린다
RISK_LIFT, VELO_DROP = 2.0, -1.5
# 선발 피로도: 마지막 선발 후 일수 -> 점수 (KBO는 월요일 휴식이라 보통 6일 간격)
STARTER_FATIGUE = {1: 100, 2: 95, 3: 90, 4: 60, 5: 20}
USAGE_FEATURES = ["절편", "평소 등판 비율(logit)", "1일 연투 뒤", "2일 연투 뒤", "3일 이상 연투 뒤",
                  "전날 투구 수/10", "전날 20구 초과분/10", "그저께 투구 수/10", "최근 7일 40구 초과분/20"]
DROP_FEATURES = ["절편", "구속 -1~-2km/h", "구속 -2~-3km/h", "구속 -3km/h 이하", "구속 기록 없음",
                 "자책 3점 이상", "구원 35구 이상", "선발"]


def _int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def day(s):
    return date.fromisoformat(s).toordinal()


def iso(d):
    return date.fromordinal(d).isoformat()


def sigmoid(x):
    return 1 / (1 + math.exp(-max(-30, min(30, x))))


def logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


# ---------- 데이터 ----------

class Season:
    """pitching.csv -> 투수별 날짜별 등판 기록, 팀별 경기일·엔트리."""

    def __init__(self, rows):
        self.apps = defaultdict(dict)  # pid -> day -> {n, start, er, outs, fb:[(kmh, n)]}
        self.names, self.hands = {}, {}
        self.team_days = defaultdict(dict)  # team -> day -> {"roster": set, "games": [...], "small": bool}
        self.games = defaultdict(list)  # day -> [(game_id, away, home, round)]
        self.starters = {}  # (game_id, team) -> pid
        seen_games = set()
        for r in rows:
            d, pid, team = day(r["game_date"]), r["pitcher_id"], r["team"]
            self.names[pid] = r["pitcher_name"]
            if r.get("hand"):
                self.hands[pid] = r["hand"]
            td = self.team_days[team].setdefault(d, {"roster": set(), "games": set(), "size": Counter()})
            td["roster"].add(pid)
            td["games"].add(r["game_id"])
            td["size"][r["game_id"]] += 1
            if r["game_id"] not in seen_games:
                seen_games.add(r["game_id"])
                home = _int(r["home"])
                away_t, home_t = (r["opponent"], team) if home else (team, r["opponent"])
                self.games[d].append((r["game_id"], away_t, home_t, r["round_code"]))
            if _int(r["used"]):
                a = self.apps[pid].setdefault(d, {"n": 0, "start": 0, "er": 0, "outs": 0, "fb": [], "team": team})
                a["n"] += _int(r["pitches"])
                a["er"] += _int(r["er"])
                a["outs"] += _int(r["outs"])
                a["team"] = team
                if _int(r["starter"]):
                    a["start"] = 1
                    self.starters[(r["game_id"], team)] = pid
                if _float(r["fb_kmh"]) and _int(r["fb_n"]):
                    a["fb"].append((float(r["fb_kmh"]), _int(r["fb_n"])))
        for team, days in self.team_days.items():
            for d, td in days.items():
                td["small"] = min(td["size"].values()) < MIN_ROSTER
        self.days = sorted({d for days in self.team_days.values() for d in days})
        self.teams = sorted(self.team_days)

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8-sig") as f:
            return cls(list(csv.DictReader(f)))

    def roster_on(self, team, d):
        """그날 경기가 있으면 그날 엔트리, 없으면 가장 최근 경기의 엔트리 (예측할 때는 이것만 안다)."""
        days = self.team_days[team]
        if d in days and not days[d]["small"]:
            return days[d]["roster"], True
        prev = [x for x in days if x < d and not days[x]["small"]]
        return (days[max(prev)]["roster"], False) if prev else (set(), False)


def state(season, pid, d):
    """d일 경기 직전까지의 기록만으로 계산한 투수 상태."""
    apps = season.apps.get(pid, {})
    prev = sorted(x for x in apps if x < d)
    s = {"y": [apps[d - k]["n"] if d - k in apps else 0 for k in (1, 2, 3, 4, 5)], "apps": len(prev)}
    streak = 0
    while d - streak - 1 in apps:
        streak += 1
    s["streak"] = streak
    s["rest"] = d - prev[-1] if prev else None
    s["p7"] = sum(apps[x]["n"] for x in prev if d - x <= 7)
    recent = prev[-10:]
    s["starter"] = bool(recent) and sum(apps[x]["start"] for x in recent) / len(recent) >= 0.5
    starts = [x for x in prev if apps[x]["start"]]
    s["since_start"] = d - starts[-1] if starts else None
    s["last"] = prev[-1] if prev else None
    s["dv"] = velo_drop(apps, prev)
    return s


def velo_drop(apps, prev):
    """마지막 등판의 주 직구 평균 구속 - 그 전 최대 8경기 평균 (km/h). 기록이 적으면 None."""
    if not prev:
        return None
    fb_last = apps[prev[-1]]["fb"]
    n_last = sum(n for _, n in fb_last)
    base = [(v, n) for x in prev[-9:-1] for v, n in apps[x]["fb"]]
    n_base = sum(n for _, n in base)
    if n_last < 5 or n_base < 30:
        return None
    return sum(v * n for v, n in fb_last) / n_last - sum(v * n for v, n in base) / n_base


# ---------- 불펜 등판 가능성 ----------

def usage_x(s, base_logit):
    y1, y2 = s["y"][0], s["y"][1]
    return [1.0, base_logit, float(s["streak"] == 1), float(s["streak"] == 2), float(s["streak"] >= 3),
            y1 / 10, max(y1 - 20, 0) / 10, y2 / 10, max(s["p7"] - 40, 0) / 20]


def rested_x(base_logit):
    return [1.0, base_logit, 0, 0, 0, 0, 0, 0, 0]


class BaseRate:
    """투수의 '쉬던 날(연투 아님)' 등판 비율을 날짜 순으로 쌓는다. 리그 평균 쪽으로 SHRINK만큼 당긴다."""

    def __init__(self, league):
        self.league = league
        self.used, self.n = Counter(), Counter()

    def logit(self, pid):
        return logit((self.used[pid] + SHRINK * self.league) / (self.n[pid] + SHRINK))

    def add(self, pid, used):
        self.used[pid] += used
        self.n[pid] += 1


def roster_days(season):
    """(day, team, pid, 등판 여부): 엔트리가 온전히 잡힌 경기일의 엔트리 투수 전원."""
    for d in season.days:
        for team in season.teams:
            td = season.team_days[team].get(d)
            if td and not td["small"]:
                for pid in sorted(td["roster"]):
                    yield d, team, pid, int(d in season.apps.get(pid, {}))


def usage_samples(season):
    """불펜 투수 표본 [(day, pid, x, y)]. 평소 등판 비율은 그날 이전 기록으로만 계산한다."""
    pool = [(d, pid, y, state(season, pid, d)) for d, _, pid, y in roster_days(season)]
    pool = [(d, pid, y, s) for d, pid, y, s in pool if s["apps"] and not s["starter"]]
    rested = [y for d, pid, y, s in pool if s["streak"] == 0 and day(SPLIT_DATE) > d]
    base = BaseRate(sum(rested) / len(rested))
    out = []
    for d in sorted({d for d, *_ in pool}):
        today = [(pid, y, s) for dd, pid, y, s in pool if dd == d]
        for pid, y, s in today:
            out.append((d, pid, usage_x(s, base.logit(pid)), y))
        for pid, y, s in today:  # 그날 결과는 다음 날부터 반영
            if s["streak"] == 0:
                base.add(pid, y)
    return out, base


def fit_logistic(X, y, l2=L2, iters=30):
    """뉴턴법 로지스틱 회귀 (절편 제외 L2). 의존성을 늘리지 않으려고 직접 푼다."""
    k = len(X[0])
    w = [0.0] * k
    for _ in range(iters):
        g = [0.0] * k
        H = [[0.0] * k for _ in range(k)]
        for xi, yi in zip(X, y):
            p = sigmoid(sum(a * b for a, b in zip(w, xi)))
            r, v = p - yi, p * (1 - p)
            for a in range(k):
                g[a] += r * xi[a]
                va = v * xi[a]
                for b in range(a, k):
                    H[a][b] += va * xi[b]
        for a in range(k):
            for b in range(a):
                H[a][b] = H[b][a]
            if a:
                g[a] += l2 * w[a]
                H[a][a] += l2
        step = solve(H, g)
        w = [wi - si for wi, si in zip(w, step)]
        if max(abs(s) for s in step) < 1e-8:
            break
    return w


def solve(A, b):
    n = len(b)
    M = [row[:] + [bi] for row, bi in zip(A, b)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[piv] = M[piv], M[c]
        if abs(M[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]


def predict(w, x):
    return sigmoid(sum(a * b for a, b in zip(w, x)))


def auc(scores, labels):
    pairs = sorted(zip(scores, labels))
    pos = sum(labels)
    neg = len(labels) - pos
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg = (i + j + 1) / 2
        rank_sum += avg * sum(l for _, l in pairs[i:j])
        i = j
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg) if pos and neg else float("nan")


def logloss(ps, ys):
    return -sum(math.log(max(p if y else 1 - p, 1e-12)) for p, y in zip(ps, ys)) / len(ys)


# ---------- 선발 ----------

def starter_table(season):
    """선발 역할 투수가 '마지막 선발 후 n일째'에 실제 선발로 나온 비율 (엔트리에 있던 날 기준)."""
    hit, tot = Counter(), Counter()
    for d, team, pid, used in roster_days(season):
        s = state(season, pid, d)
        if not s["starter"] or s["since_start"] is None:
            continue
        k = min(s["since_start"], 10)
        tot[k] += 1
        hit[k] += int(used and season.apps[pid][d]["start"])
    return {k: (hit[k] + 0.5) / (tot[k] + 1) for k in range(1, 11)}, tot


def rotation(season, team, d, table, lookback=21):
    """팀의 최근 선발 투수들을 '오늘 선발로 나올 비율' 순으로. [(pid, 비율, 마지막 선발 후 일수)]"""
    cands = {}
    for pid, apps in season.apps.items():
        starts = [x for x, a in apps.items() if a["start"] and a["team"] == team and d - lookback <= x < d]
        if starts:
            since = d - max(starts)
            # 등판 후 다른 팀으로 옮긴 경우 제외
            if max(x for x in apps if x < d) and apps[max(x for x in apps if x < d)]["team"] == team:
                cands[pid] = since
    ranked = [(pid, table.get(min(since, 10), 0), since) for pid, since in cands.items()]
    return sorted(ranked, key=lambda t: (-t[1], -t[2]))


# ---------- 말소(부상 의심) 위험 ----------

def drop_x(dv, er, n, start):
    return [1.0, float(dv is not None and -2 < dv <= -1), float(dv is not None and -3 < dv <= -2),
            float(dv is not None and dv <= -3), float(dv is None), float(er >= 3), float(not start and n >= 35),
            float(start)]


def drop_samples(season):
    """등판 하나마다: 그 뒤 팀의 다음 DROP_WINDOW경기 안에 엔트리에서 빠졌는가."""
    out = []
    for pid, apps in season.apps.items():
        ds = sorted(apps)
        for i, d in enumerate(ds):
            a = apps[d]
            team_days = sorted(x for x, td in season.team_days[a["team"]].items() if x > d and not td["small"])
            nxt = team_days[:DROP_WINDOW]
            if len(nxt) < DROP_WINDOW:
                continue  # 시즌 끝
            dropped = int(any(pid not in season.team_days[a["team"]][x]["roster"] for x in nxt))
            out.append((d, pid, drop_x(velo_drop(apps, ds[:i + 1]), a["er"], a["n"], a["start"]), dropped))
    return out


# ---------- 모델 묶음 ----------

class Workload:
    def __init__(self, season, split=SPLIT_DATE):
        self.season = season
        split_d = day(split)
        samples, _ = usage_samples(season)
        tr = [(x, y) for d, _, x, y in samples if d < split_d]
        te = [(x, y) for d, _, x, y in samples if d >= split_d]
        w = fit_logistic([x for x, _ in tr], [y for _, y in tr])
        wb = fit_logistic([x[:2] for x, _ in tr], [y for _, y in tr])
        ps = [predict(w, x) for x, _ in te]
        pb = [predict(wb, x[:2]) for x, _ in te]
        ys = [y for _, y in te]
        self.eval = {"usage": {"n_train": len(tr), "n_test": len(te), "auc": auc(ps, ys), "auc_base": auc(pb, ys),
                               "logloss": logloss(ps, ys), "logloss_base": logloss(pb, ys),
                               "rate": sum(ys) / len(ys)}}
        self.usage_w = fit_logistic([x for _, _, x, _ in samples], [y for *_, y in samples])
        self.usage_rows = samples

        self.start_table, start_n = starter_table(season)
        hits = top2 = n = 0
        for d in season.days:
            if d < split_d:
                continue
            for gid, away, home, _ in season.games[d]:
                for team in (away, home):
                    actual = season.starters.get((gid, team))
                    if not actual:
                        continue
                    ranked = [pid for pid, *_ in rotation(season, team, d, self.start_table)]
                    n += 1
                    hits += bool(ranked) and ranked[0] == actual
                    top2 += actual in ranked[:2]
        self.eval["starter"] = {"n": n, "acc": hits / n if n else None, "top2": top2 / n if n else None,
                                "table": {k: round(v, 3) for k, v in self.start_table.items()},
                                "table_n": dict(start_n)}

        drops = drop_samples(season)
        dtr = [(x, y) for d, _, x, y in drops if d < split_d]
        dte = [(x, y) for d, _, x, y in drops if d >= split_d]
        wd = fit_logistic([x for x, _ in dtr], [y for _, y in dtr])
        self.eval["drop"] = {"n_train": len(dtr), "n_test": len(dte),
                             "auc": auc([predict(wd, x) for x, _ in dte], [y for _, y in dte]),
                             "rate": sum(y for *_, y in drops) / len(drops),
                             "by_velo": self._rates(drops)}
        self.drop_w = fit_logistic([x for *_, x, _ in drops], [y for *_, y in drops])
        self.drop_base = sum(y for *_, y in drops) / len(drops)
        self.base_rate = self._final_base_rate()

    @staticmethod
    def _rates(drops):
        bins = [("-3 이하", 3), ("-3~-2", 2), ("-2~-1", 1)]
        out = {}
        for label, k in bins:
            ys = [y for _, _, x, y in drops if x[k]]
            out[label] = (sum(ys) / len(ys), len(ys)) if ys else (None, 0)
        ys = [y for _, _, x, y in drops if not any(x[1:5])]
        out["-1 초과"] = (sum(ys) / len(ys), len(ys))
        return out

    def _final_base_rate(self):
        rested = [y for _, _, x, y in self.usage_rows if not any(x[2:5])]
        base = BaseRate(sum(rested) / len(rested))
        for d, team, pid, used in roster_days(self.season):
            s = state(self.season, pid, d)
            if s["apps"] and not s["starter"] and s["streak"] == 0:
                base.add(pid, used)
        return base

    def assess(self, pid, d, base=None):
        """d일 경기에 이 투수가 나와도 되는지."""
        s = state(self.season, pid, d)
        base = base or self.base_rate
        out = {"pid": pid, "state": s, "reasons": []}
        if s["starter"]:
            since = s["since_start"]
            out["role"] = "선발"
            out["fatigue"] = STARTER_FATIGUE.get(since, 0) if since is not None else 0
            out["p_start"] = self.start_table.get(min(since, 10), 0) if since else None
            verdict = 2 if since is not None and since <= 3 else 1 if since == 4 else 0
            if since is not None and since <= 5:
                out["reasons"].append(f"선발 후 {since}일째")
            out["ratio"] = None
        else:
            out["role"] = "불펜"
            bl = base.logit(pid)
            p, p0 = predict(self.usage_w, usage_x(s, bl)), predict(self.usage_w, rested_x(bl))
            ratio = min(p / p0, 1.0) if p0 else 1.0
            out.update({"p_use": p, "p_rested": p0, "ratio": ratio, "fatigue": round(100 * (1 - ratio))})
            verdict = 2 if ratio < RATIO_REST else 1 if ratio < RATIO_CAUTION else 0
            if s["streak"] >= 2:
                out["reasons"].append(f"{s['streak']}연투")
            elif s["streak"] == 1:
                out["reasons"].append(f"전날 {s['y'][0]}구")
            if s["p7"] > 40:
                out["reasons"].append(f"최근 7일 {s['p7']}구")
        # 말소 위험은 직전 등판이 최근(8일 안, 선발 간격 포함)일 때만 의미가 있다
        risk = None
        if s["last"] is not None and d - s["last"] <= 8:
            a = self.season.apps[pid][s["last"]]
            risk = predict(self.drop_w, drop_x(s["dv"], a["er"], a["n"], a["start"]))
        out["risk"] = risk
        if risk is not None and s["dv"] is not None and s["dv"] <= VELO_DROP and risk >= RISK_LIFT * self.drop_base:
            out["reasons"].append(f"직전 구속 {s['dv']:+.1f}km/h")
            verdict = max(verdict, 1)
        out["verdict"] = verdict
        return out

    def team_report(self, team, d):
        roster, actual = self.season.roster_on(team, d)
        players = sorted((self.assess(pid, d) for pid in roster), key=lambda a: (a["role"] != "선발", a["verdict"], a["fatigue"]))
        rot = rotation(self.season, team, d, self.start_table)
        return {"roster_actual": actual, "players": players, "rotation": rot}


# ---------- 보고서·웹 ----------

def fatigue_outcomes(season):
    """불펜 등판을 직전 상태별로 묶어 구속 차이와 평균자책을 낸다."""
    groups = {"연투 아님": [], "1일 연투": [], "2일 이상 연투": [], "전날 25구 이상": []}
    for pid, apps in season.apps.items():
        ds = sorted(apps)
        for i, d in enumerate(ds):
            a = apps[d]
            if a["start"] or i == 0:
                continue
            s = state(season, pid, d)
            dv = velo_drop(apps, ds[:i + 1])
            key = "2일 이상 연투" if s["streak"] >= 2 else "1일 연투" if s["streak"] == 1 else "연투 아님"
            for k in {key} | ({"전날 25구 이상"} if s["y"][0] >= 25 else set()):
                groups[k].append((dv, a["er"], a["outs"]))
    rows = []
    for k, g in groups.items():
        dvs = [dv for dv, *_ in g if dv is not None]
        outs = sum(o for *_, o in g)
        rows.append(f"| {k} | {len(g):,} | {sum(dvs) / len(dvs):+.2f}km/h | {sum(e for _, e, _ in g) * 27 / outs:.2f} |"
                    if dvs and outs else f"| {k} | {len(g):,} | – | – |")
    return rows


def write_report(m, path):
    ev = m.eval
    u, s, dr = ev["usage"], ev["starter"], ev["drop"]
    names = USAGE_FEATURES
    lines = [
        "# 투수 피로도·등판 가능 여부 모델", "",
        f"`python -m kbo_crawler.workload` 로 생성 ({datetime.now():%Y-%m-%d %H:%M}). "
        f"데이터: 2026 정규시즌·포스트시즌 {len(m.season.days)}개 경기일, 마지막 {iso(m.season.days[-1])}. "
        f"검증은 {SPLIT_DATE} 이전으로 학습하고 이후를 맞혀 본 결과입니다.", "",
        "부상 기록은 공개 데이터에 없어서, 실제로 관찰되는 두 가지를 정답으로 씁니다: "
        "**엔트리에 있던 불펜 투수가 그날 나왔는가**(코칭스태프의 판단), "
        "**등판 뒤 3경기 안에 엔트리에서 빠졌는가**(부상·부진 말소).", "",
        "## 1. 불펜 등판 가능성", "",
        f"- 학습 {u['n_train']:,}건, 검증 {u['n_test']:,}건 (엔트리에 있던 불펜 투수 x 경기일). 검증 구간 등판 비율 {u['rate']:.1%}",
        f"- AUC **{u['auc']:.3f}** (투수별 평소 등판 비율만 쓴 기준 모델 {u['auc_base']:.3f}), "
        f"로그 손실 {u['logloss']:.4f} (기준 {u['logloss_base']:.4f})", "",
        "| 변수 | 계수 |", "|---|---:|",
        *[f"| {n} | {w:+.3f} |" for n, w in zip(names, m.usage_w)], "",
        "가용도 = (지금 상태의 등판 확률) / (같은 투수가 연투·최근 부하 없이 쉬었을 때의 등판 확률). "
        f"피로도 = 100 x (1 - 가용도). 가용도 {RATIO_REST} 미만은 휴식 권장, {RATIO_CAUTION} 미만은 주의.", "",
        "실제 KBO 팀들이 상태별로 불펜 투수를 쓴 비율:", "",
        "| 상태 | 표본 | 실제 등판 비율 |", "|---|---:|---:|",
    ]
    rows = m.usage_rows
    groups = [("연투 아님", lambda x: not any(x[2:5])), ("1일 연투 뒤", lambda x: x[2]), ("2일 연투 뒤", lambda x: x[3]),
              ("3일 이상 연투 뒤", lambda x: x[4]), ("전날 30구 이상", lambda x: x[5] >= 3)]
    for label, f in groups:
        ys = [y for _, _, x, y in rows if f(x)]
        if ys:
            lines.append(f"| {label} | {len(ys):,} | {sum(ys) / len(ys):.1%} |")
    lines += ["", "지친 상태로 실제 등판했을 때의 결과 (불펜, 주 직구 구속은 그 투수의 최근 8경기 평균 대비):", "",
              "| 상태 | 등판 | 구속 차이 | 평균자책 |", "|---|---:|---:|---:|", *fatigue_outcomes(m.season), "",
              "팀들이 지친 투수를 이미 잘 쉬게 해서, 실제로 나온 경기의 성적 차이는 크지 않습니다. "
              "그래서 피로도는 성적 예측이 아니라 '코칭스태프라면 이 상태에서 얼마나 아끼는가'로 매깁니다."]
    lines += ["", "## 2. 예상 선발", "",
              f"- 검증 구간 {s['n']}경기(팀 기준)에서 1순위 적중 **{s['acc']:.1%}**, 2순위 안 {s['top2']:.1%}",
              "- 후보: 팀의 최근 21일 선발 투수. 순위: 마지막 선발 후 일수별 실제 선발 비율", "",
              "| 마지막 선발 후 | 표본 | 그날 선발 비율 |", "|---|---:|---:|",
              *[f"| {k}일{'+' if k == 10 else ''} | {s['table_n'].get(k, 0):,} | {v:.1%} |" for k, v in s["table"].items()], "",
              "## 3. 엔트리 말소(부상 의심) 위험", "",
              f"- 등판 {dr['n_train'] + dr['n_test']:,}건 중 다음 3경기 안 말소 {dr['rate']:.1%}. 검증 AUC {dr['auc']:.3f}",
              "- 말소에는 부진에 따른 2군행도 들어 있습니다. 그래서 판정에는 구속 저하가 함께 있을 때만 '부상 의심 신호'로 씁니다.", "",
              "| 직전 등판 주 직구 구속 (최근 8경기 평균 대비) | 표본 | 3경기 안 말소 |", "|---|---:|---:|",
              *[f"| {k} | {n:,} | {r:.1%} |" for k, (r, n) in dr["by_velo"].items() if r is not None], "",
              "| 변수 | 계수 |", "|---|---:|", *[f"| {n} | {w:+.3f} |" for n, w in zip(DROP_FEATURES, m.drop_w)], "",
              "## 판정 규칙", "",
              f"- 불펜: 가용도 {RATIO_REST} 미만 휴식 권장 / {RATIO_CAUTION} 미만 주의 / 그 외 가능",
              "- 선발: 마지막 선발 후 3일 이내 휴식 권장 / 4일 주의 / 5일 이상 가능",
              f"- 공통: 직전 등판 구속이 {VELO_DROP}km/h 이상 떨어졌고 말소 위험이 평소의 {RISK_LIFT}배 이상이면 최소 '주의'",
              "- 경계값은 모델 숫자에 붙인 기준이며 의학적 판단이 아닙니다.", ""]
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def upcoming_games(start, days=7):
    """네이버 일정에서 아직 데이터에 없는 경기와 예고 선발 이름. 접속이 안 되면 빈 목록."""
    from .client import NaverSportsClient
    from .parse import is_official_round

    try:
        data = NaverSportsClient(cache_dir=None, retries=1).schedule(iso(start), iso(start + days - 1))
    except Exception as e:  # 네트워크 문제로 웹 데이터 생성 전체가 멈추지 않게 한다
        print(f"일정을 가져오지 못했습니다 ({type(e).__name__}). 다음 경기일 예측은 팀을 직접 고르는 방식만 제공합니다.")
        return []
    out = []
    for g in (data.get("result") or {}).get("games") or []:
        if g.get("categoryId") not in (None, "kbo") or g.get("cancel") or not is_official_round(g.get("roundCode")):
            continue
        # 예고 선발 필드 이름은 응답마다 다를 수 있어 'starter'가 들어간 이름 필드를 찾는다
        starter = lambda side: next((v for k, v in g.items() if k.lower().startswith(side) and "starter" in k.lower()
                                     and k.lower().endswith("name") and v), None)
        out.append((day(g["gameDate"]), g.get("gameId"), g.get("awayTeamName"), g.get("homeTeamName"),
                    g.get("roundCode"), starter("away"), starter("home")))
    return out


def export_web(m, path, extra_days=1, upcoming=()):
    """경기일마다 10개 팀의 투수 상태를 미리 계산해 저장한다 (마지막 경기일 다음 날 = 다음 경기 예측 포함).
    upcoming: upcoming_games() 결과. 데이터 이후 날짜의 경기와 예고 선발을 붙인다."""
    season = m.season
    pids = sorted({pid for team in season.teams for td in season.team_days[team].values() for pid in td["roster"]})
    pidx = {p: i for i, p in enumerate(pids)}
    last_team = {}
    for pid, apps in season.apps.items():
        last_team[pid] = apps[max(apps)]["team"]
    for team in season.teams:
        for d in sorted(season.team_days[team]):
            for pid in season.team_days[team][d]["roster"]:
                last_team.setdefault(pid, team)
    tidx = {t: i for i, t in enumerate(season.teams)}
    up_days = {d for d, *_ in upcoming if d > season.days[-1]}
    days = season.days[7:] + sorted({season.days[-1] + k for k in range(1, extra_days + 1)} | up_days)
    by_name = defaultdict(dict)  # 팀 -> 이름 -> 투수 id (예고 선발 이름 맞추기)
    for pid, team in last_team.items():
        by_name[team][season.names[pid]] = pid
    out_days = {}
    for d in days:
        teams = {}
        for team in season.teams:
            rep = m.team_report(team, d)
            if not rep["players"]:
                continue
            rows = []
            for a in rep["players"]:
                s = a["state"]
                rows.append([pidx[a["pid"]], a["verdict"], a["fatigue"], int(a["role"] == "선발"),
                             None if a["ratio"] is None else round(a["ratio"] * 100),
                             None if a["risk"] is None else round(a["risk"] * 1000),
                             s["streak"], s["rest"], s["since_start"], s["y"],
                             None if s["dv"] is None else round(s["dv"] * 10), s["p7"], "·".join(a["reasons"])])
            teams[tidx[team]] = {"actual": int(rep["roster_actual"]), "rotation": [[pidx[p], round(r * 100), since] for p, r, since in rep["rotation"][:3]],
                                 "players": rows}
        games = [[gid, tidx[away], tidx[home], rnd, pidx.get(season.starters.get((gid, away))), pidx.get(season.starters.get((gid, home)))]
                 for gid, away, home, rnd in season.games.get(d, [])]
        # 데이터 이후 경기: 실제 선발 자리에 예고 선발을 넣고 announced=1로 표시
        for ud, gid, away, home, rnd, sa, sh in upcoming:
            if ud == d and d > season.days[-1] and away in tidx and home in tidx:
                games.append([gid, tidx[away], tidx[home], rnd, pidx.get(by_name[away].get(sa)), pidx.get(by_name[home].get(sh)), 1])
        out_days[iso(d)] = {"games": games, "teams": teams}
    ev = m.eval
    data = {
        "meta": {"generated": datetime.now().isoformat(timespec="minutes"), "last_game_day": iso(season.days[-1]),
                 "split": SPLIT_DATE, "verdicts": VERDICTS,
                 "rules": {"ratio_rest": RATIO_REST, "ratio_caution": RATIO_CAUTION, "risk_lift": RISK_LIFT, "velo_drop": VELO_DROP},
                 "drop_base": round(m.drop_base, 4),
                 "eval": {"usage": {k: round(v, 4) for k, v in ev["usage"].items()},
                          "starter": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in ev["starter"].items() if k in ("n", "acc", "top2")},
                          "drop": {"auc": round(ev["drop"]["auc"], 4), "rate": round(ev["drop"]["rate"], 4),
                                   "by_velo": {k: [None if r is None else round(r, 4), n] for k, (r, n) in ev["drop"]["by_velo"].items()}}},
                 "row": ["pitcher", "verdict", "fatigue", "starter", "ratio%", "risk‰", "streak", "rest", "since_start",
                         "pitches_d-1..d-5", "velo_drop_x10", "p7", "reasons"]},
        "teams": season.teams,
        "pitchers": [[season.names[p], tidx[last_team[p]], season.hands.get(p, "")] for p in pids],
        "days": out_days,
    }
    Path(path).write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description="투수 피로도·등판 가능 여부 모델")
    ap.add_argument("pitching", nargs="?", default="datasets/kbo_games/pitching.csv")
    ap.add_argument("--report", default="docs/workload_report.md")
    ap.add_argument("--web", default="web/workload.json")
    ap.add_argument("--schedule", type=int, default=0, metavar="DAYS",
                    help="마지막 경기일 다음 날부터 DAYS일 동안의 네이버 일정·예고 선발을 붙인다 (네트워크 필요)")
    args = ap.parse_args(argv)
    m = Workload(Season.load(args.pitching))
    u, s, dr = m.eval["usage"], m.eval["starter"], m.eval["drop"]
    print(f"불펜 등판 AUC {u['auc']:.3f} (기준 {u['auc_base']:.3f}), 예상 선발 적중 {s['acc']:.1%} (2순위 안 {s['top2']:.1%}), "
          f"말소 위험 AUC {dr['auc']:.3f}")
    if args.report:
        write_report(m, args.report)
        print(f"보고서 -> {args.report}")
    if args.web:
        upcoming = upcoming_games(m.season.days[-1] + 1, args.schedule) if args.schedule else []
        data = export_web(m, args.web, upcoming=upcoming)
        print(f"웹 데이터 -> {args.web} ({Path(args.web).stat().st_size / 1e6:.1f}MB, {len(data['days'])}일)")


if __name__ == "__main__":
    main()
