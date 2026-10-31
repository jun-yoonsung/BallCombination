"""수집한 투구 CSV를 웹 탐색기(web/index.html)용 압축 JSON으로 변환한다.

예)
  python -m kbo_crawler.export_web datasets/kbo_pitches/2026-*.csv --out web/pitches.json

20만 개 넘는 투구를 브라우저에서 바로 다루기 위해 열(column) 단위 정수 배열로 저장한다.
- 문자열(투수, 타자, 구종, 날짜 등)은 사전 인덱스로 바꾼다.
- plate_x, plate_z는 1/100 ft 단위 정수.
- plate_z는 타자마다 다른 스트라이크존을 리그 평균 존(1.6~3.3ft)에 맞춰 정규화한다.
- sd는 투구 직전 점수차(투수팀 기준).
- r은 타석 결과 인덱스*2 + 결정구 여부(1). 결과가 없는 타석은 null.
- 카운트, 아웃, 주자, 타석, 결과, 포스트시즌 여부는 비트로 묶어 한 정수(s)에 담는다.
- mx, mz는 무브먼트(cm, 포수 시점 수평 +가 1루 쪽 / 중력 제외 수직).
- lineups는 팀 인덱스별 예상 선발 타순 {games, order: 1~9번 타자 인덱스} (expected_lineups).
- 투수의 투구 손(R/L)은 측정 시작 지점의 좌우 위치(x0) 중앙값 부호로 판단한다.
- pv는 같은 타석의 직전 구종(첫 공이면 null), zn은 코스(predict.zone_of, 0~12),
  rv는 투구 가치(타자 기준 득점 x1000). model에는 예측 상수, 타자별 구종 보정, 검증 성능이 들어간다.

궤적 파라미터는 용량이 커서 투수의 마지막 소속팀별로 traj/{팀 인덱스}.json에 따로 저장하고,
웹에서는 그 팀 투수를 볼 때만 불러온다. y0(측정 시작 지점)는 2026년 7월 중순 50ft에서 55ft로 바뀌었으므로
공마다 저장해야 한다.
"""

import argparse
import glob
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import predict
from .physics import from_row, movement

ZONE_TOP, ZONE_BOT = 3.33, 1.61
TRAJ_KEYS = ("x0", "z0", "vx0", "vy0", "vz0", "ax", "ay", "az", "y0")  # 1/100 단위 정수로 저장
LINEUP_GAMES = 20  # 예상 라인업은 팀의 최근 20경기 선발 타순으로 정한다
RESULTS = ["B", "T", "S", "F", "H", "W", "V"]
PA_RESULTS = ["삼진", "볼넷", "고의4구", "사구", "1루타", "2루타", "3루타", "홈런", "땅볼", "플라이",
              "라인드라이브", "병살타", "희생번트", "희생플라이", "실책출루", "야수선택", "번트아웃", "기타"]


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def expected_lineups(rows, recent=LINEUP_GAMES):
    """팀별 예상 선발 라인업(1~9번 타자 id)과 근거가 된 경기 수.

    경기·공격 반(half)마다 처음 타석에 선 서로 다른 타자 9명을 그 경기 선발 1~9번으로 본다.
    팀의 최근 경기들에서 (타자, 타순) 선발 횟수를 세고, 많은 쌍부터 겹치지 않게 채운다.
    """
    games = {}  # (팀, 경기) -> (날짜, [타자 id 순서])
    for r in rows:
        team = r["away_team"] if r["half"] == "top" else r["home_team"]
        date, order = games.setdefault((team, r["game_id"]), (r["game_date"], []))
        if len(order) < 9 and r["batter_id"] not in order:
            order.append(r["batter_id"])
    by_team = {}
    for (team, gid), (date, order) in games.items():
        if len(order) == 9:
            by_team.setdefault(team, []).append((date, gid, order))
    lineups = {}
    for team, gs in by_team.items():
        gs = sorted(gs)[-recent:]
        starts = Counter((b, slot) for _, _, order in gs for slot, b in enumerate(order))
        lineup, used = [None] * 9, set()
        for (b, slot), _ in sorted(starts.items(), key=lambda kv: -kv[1]):
            if lineup[slot] is None and b not in used:
                lineup[slot] = b
                used.add(b)
        lineups[team] = (lineup, len(gs))
    return lineups


class Index:
    def __init__(self):
        self.items, self.pos = [], {}

    def __call__(self, key):
        if key not in self.pos:
            self.pos[key] = len(self.items)
            self.items.append(key)
        return self.pos[key]


def build(paths):
    teams, types, dates = Index(), Index(), Index()
    pitchers, batters = Index(), Index()
    pitcher_info, batter_info = {}, {}
    cols = {k: [] for k in ("p", "b", "t", "v", "x", "z", "s", "d", "sd", "r", "mx", "mz", "pv", "zn", "rv")}
    traj = []  # (투구 인덱스, 투수 인덱스, 파라미터)

    rows = predict.annotate(predict.load_rows(paths))  # 구종 없는 공은 빠지고 경기·seqno 순으로 정렬됨
    run_value = predict.run_values(rows)

    for r in rows:
        top = r["half"] == "top"
        p_team = r["home_team"] if top else r["away_team"]
        b_team = r["away_team"] if top else r["home_team"]
        p = pitchers(r["pitcher_id"])
        b = batters(r["batter_id"])
        # 시즌 중 이적한 선수는 마지막 소속팀으로 표시
        pitcher_info[p] = (r["pitcher_name"], teams(p_team))
        batter_info[b] = (r["batter_name"], teams(b_team))

        x, z = _float(r["plate_x"]), _float(r["plate_z"])
        top_sz, bot_sz = _float(r["sz_top"]), _float(r["sz_bot"])
        if z is not None and top_sz and bot_sz and top_sz > bot_sz:
            z = ZONE_BOT + (z - bot_sz) / (top_sz - bot_sz) * (ZONE_TOP - ZONE_BOT)

        bases = _int(r["on_1b"]) | _int(r["on_2b"]) << 1 | _int(r["on_3b"]) << 2
        result = RESULTS.index(r["pitch_result"]) if r["pitch_result"] in RESULTS else 15
        packed = (
            min(_int(r["balls"]), 3)
            | min(_int(r["strikes"]), 2) << 2
            | min(_int(r["outs"]), 2) << 4
            | bases << 6
            | (1 if r["stance"] == "L" else 0) << 9
            | result << 10
            | (1 if r["round_code"].startswith("kbo_ps") else 0) << 14
        )

        cols["p"].append(p)
        cols["b"].append(b)
        cols["t"].append(types(r["pitch_type"]))
        cols["v"].append(_int(r["speed_kmh"], None))
        cols["x"].append(None if x is None else round(x * 100))
        cols["z"].append(None if z is None else round(z * 100))
        cols["s"].append(packed)
        cols["d"].append(dates(r["game_date"]))
        cols["sd"].append(_int(r["score_diff"], None))
        pa = r.get("pa_result")
        cols["r"].append(PA_RESULTS.index(pa) * 2 + _int(r.get("pa_last_pitch")) if pa in PA_RESULTS else None)

        params = from_row(r)
        mv = movement(params) if params else None
        cols["mx"].append(None if mv is None else round(mv[0]))
        cols["mz"].append(None if mv is None else round(mv[1]))
        cols["pv"].append(None if r["_prev"] == "-" else types(r["_prev"]))
        cols["zn"].append(r["_zone"])
        cols["rv"].append(None if r["_rv"] is None else round(r["_rv"] * 1000))
        if params:
            traj.append((len(cols["p"]) - 1, p, params))

    # 구종은 많이 던진 순서로 정렬해서 인덱스를 다시 매긴다.
    freq = Counter(cols["t"])
    order = sorted(range(len(types.items)), key=lambda i: -freq[i])
    remap = {old: new for new, old in enumerate(order)}
    cols["t"] = [remap[t] for t in cols["t"]]
    cols["pv"] = [None if t is None else remap[t] for t in cols["pv"]]
    type_names = [types.items[i] for i in order]

    type_model = predict.TypeModel().fit(rows)
    ev = predict.evaluate(rows)
    batter_ratio = [None] * len(batters.items)
    for bid, ratio in type_model.batter_ratio.items():
        if bid in batters.pos:
            batter_ratio[batters.pos[bid]] = [round(ratio.get(t, 1.0), 5) for t in type_names]

    # 투구 손은 예측 모델과 같은 값을 쓴다 (predict.annotate)
    hands = {pitchers.pos[r["pitcher_id"]]: r["_hand"] for r in rows}
    traj_by_team = {}
    for i, p, params in traj:
        t = traj_by_team.setdefault(pitcher_info[p][1], {"i": [], **{k: [] for k in TRAJ_KEYS}})
        t["i"].append(i)
        for k in TRAJ_KEYS:
            t[k].append(round(params[k] * 100))

    lineups = {teams.pos[team]: {"games": n, "order": [batters.pos[b] for b in lineup]}
               for team, (lineup, n) in expected_lineups(rows).items()}

    data = {
        "meta": {
            "generated": datetime.now().isoformat(timespec="minutes"),
            "count": len(cols["p"]),
            "games": len({r["game_id"] for r in rows}),
            "zone": [ZONE_BOT, ZONE_TOP],
            "packed": "balls:2 strikes:2 outs:2 bases:3 lefty:1 result:4 post:1",
        },
        "teams": teams.items,
        "types": type_names,
        "results": RESULTS,
        "pa_results": PA_RESULTS,
        "dates": dates.items,
        "pitchers": [[*pitcher_info[i], hands.get(i)] for i in range(len(pitchers.items))],
        "batters": [list(batter_info[i]) for i in range(len(batters.items))],
        "lineups": [lineups.get(i) for i in range(len(teams.items))],
        "cols": cols,
        "model": {
            "type_alphas": predict.TYPE_ALPHAS,
            "zone_alpha_league": predict.ZONE_ALPHA_LEAGUE,
            "zone_alpha_pitcher": predict.ZONE_ALPHA_PITCHER,
            "value_alpha": predict.VALUE_ALPHA,
            "zone_box": [predict.ZONE_BOT, predict.ZONE_TOP, predict.ZONE_HALF_W],
            "batter_ratio": batter_ratio,
            "count_value": {k: round(v, 4) for k, v in run_value["count_value"].items()},
            "pa_value": {k: round(v, 4) for k, v in run_value["weights"].items()},
            "eval": {k: ev[k] for k in ("split", "n_train", "n_test", "type", "zone")},
        },
    }
    return data, traj_by_team


def main(argv=None):
    ap = argparse.ArgumentParser(description="투구 CSV -> 웹 탐색기용 JSON")
    ap.add_argument("csv", nargs="+", help="투구 CSV 경로 (glob 가능)")
    ap.add_argument("--out", default="web/pitches.json")
    args = ap.parse_args(argv)

    paths = sorted({p for pattern in args.csv for p in glob.glob(pattern)})
    data, traj_by_team = build(paths)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    dump = lambda obj, path: path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    dump(data, out)
    print(f"{len(paths)}개 파일, 투구 {data['meta']['count']}개 -> {out} ({out.stat().st_size / 1e6:.1f}MB)")
    traj_dir = out.parent / "traj"
    traj_dir.mkdir(exist_ok=True)
    for team, t in traj_by_team.items():
        dump(t, traj_dir / f"{team}.json")
    size = sum(f.stat().st_size for f in traj_dir.glob("*.json")) / 1e6
    print(f"궤적 {sum(len(t['i']) for t in traj_by_team.values())}개 -> {traj_dir}/ (팀 {len(traj_by_team)}개, {size:.1f}MB)")


if __name__ == "__main__":
    main()
