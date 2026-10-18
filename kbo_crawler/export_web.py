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
- 투수의 투구 손(R/L)은 측정 시작 지점의 좌우 위치(x0) 중앙값 부호로 판단한다.

궤적 파라미터는 용량이 커서 투수의 마지막 소속팀별로 traj/{팀 인덱스}.json에 따로 저장하고,
웹에서는 그 팀 투수를 볼 때만 불러온다. y0는 항상 50ft라서 저장하지 않는다.
"""

import argparse
import csv
import glob
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import median

from .physics import from_row, movement

ZONE_TOP, ZONE_BOT = 3.33, 1.61
TRAJ_KEYS = ("x0", "z0", "vx0", "vy0", "vz0", "ax", "ay", "az")  # 1/100 단위 정수로 저장
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
    cols = {k: [] for k in ("p", "b", "t", "v", "x", "z", "s", "d", "sd", "r", "mx", "mz")}
    release_x = {}  # 투수별 x0 목록 (투구 손 판단용)
    traj = []  # (투구 인덱스, 투수 인덱스, 파라미터)

    rows = []
    for path in paths:
        with open(path, encoding="utf-8-sig") as f:
            rows.extend(csv.DictReader(f))
    rows.sort(key=lambda r: (r["game_date"], r["game_id"], _int(r["seqno"])))

    for r in rows:
        if not r["pitch_type"]:
            continue
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
        if params:
            release_x.setdefault(p, []).append(params["x0"])
            traj.append((len(cols["p"]) - 1, p, params))

    # 구종은 많이 던진 순서로 정렬해서 인덱스를 다시 매긴다.
    freq = Counter(cols["t"])
    order = sorted(range(len(types.items)), key=lambda i: -freq[i])
    remap = {old: new for new, old in enumerate(order)}
    cols["t"] = [remap[t] for t in cols["t"]]

    hands = {p: ("L" if median(xs) > 0 else "R") for p, xs in release_x.items()}
    traj_by_team = {}
    for i, p, params in traj:
        t = traj_by_team.setdefault(pitcher_info[p][1], {"i": [], **{k: [] for k in TRAJ_KEYS}})
        t["i"].append(i)
        for k in TRAJ_KEYS:
            t[k].append(round(params[k] * 100))

    data = {
        "meta": {
            "generated": datetime.now().isoformat(timespec="minutes"),
            "count": len(cols["p"]),
            "games": len({r["game_id"] for r in rows}),
            "zone": [ZONE_BOT, ZONE_TOP],
            "packed": "balls:2 strikes:2 outs:2 bases:3 lefty:1 result:4 post:1",
            "traj_y0": 50.0,
        },
        "teams": teams.items,
        "types": [types.items[i] for i in order],
        "results": RESULTS,
        "pa_results": PA_RESULTS,
        "dates": dates.items,
        "pitchers": [[*pitcher_info[i], hands.get(i)] for i in range(len(pitchers.items))],
        "batters": [list(batter_info[i]) for i in range(len(batters.items))],
        "cols": cols,
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
