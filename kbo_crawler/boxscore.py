"""경기 기록(박스스코어)과 그날 엔트리를 문자중계 원본에서 뽑아 CSV로 저장한다.

예)
  python -m kbo_crawler.boxscore "datasets/kbo_pitches/2026-*.csv" --out datasets/kbo_games

네이버 문자중계 응답(textRelayData)에는 투구 외에도 양 팀의
- homeLineup/awayLineup: 그 경기에 나온 선수의 기록 (투수: 이닝·투구 수·자책 등, 타자: 타순·타석 결과)
- homeEntry/awayEntry: 엔트리에 있었지만 나오지 않은 선수
가 들어 있다. 두 목록을 합치면 그날 1군 엔트리의 투수 전원을 알 수 있어서, "쉬었는지"와
"엔트리에 없었는지"를 구분할 수 있다 (workload.py의 등판 가능성 모델이 이것을 쓴다).

크롤러(crawl.py)가 저장한 원본 캐시(data/raw/{gameId}/inning_XX.json)를 다시 읽기만 하므로
네트워크 요청이 없다. 경기 날짜·팀 이름은 투구 CSV에서 가져오고, 그 CSV에 있는 경기(정규시즌·포스트시즌)만 쓴다.

출력
- pitching.csv: 경기별 투수 한 줄. 엔트리에만 있고 안 나온 투수는 used=0.
  fb_kmh/fb_n은 그 투수의 주 직구 계열(직구·투심·싱커 중 시즌에 가장 많이 던진 것) 평균 구속과 개수.
- batting.csv: 경기별 선발·교체 타자 한 줄 (타순 bat_order, 같은 타순 안 순서 seqno).
"""

import argparse
import csv
import glob
import json
from collections import Counter, defaultdict
from pathlib import Path

FASTBALLS = ("직구", "투심", "싱커")

PITCHING_COLUMNS = ["game_date", "round_code", "game_id", "team", "opponent", "home", "pitcher_id", "pitcher_name",
                    "hand", "used", "order", "starter", "outs", "pitches", "hit", "bb", "hbp", "kk", "hr", "run", "er",
                    "fb_kmh", "fb_n"]
BATTING_COLUMNS = ["game_date", "round_code", "game_id", "team", "opponent", "home", "bat_order", "seqno", "batter_id",
                   "batter_name", "pos", "pa", "ab", "hit", "hr", "bb", "so", "rbi", "run"]


def _int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def innings_to_outs(inn):
    """'6.2' -> 20. 네이버는 이닝을 '정수.나머지 아웃' 문자열로 준다."""
    if inn in (None, ""):
        return 0
    whole, _, frac = str(inn).partition(".")
    return _int(whole) * 3 + _int(frac[:1] or 0)


def _hand(p):
    style = p.get("pitchingStyle") or ""
    if style:
        return "L" if style.startswith("좌") else "R"
    hit = p.get("hitType") or ""
    return "L" if hit.startswith("좌") else ("R" if hit else "")


def parse_box(data):
    """문자중계 응답 하나 -> {'home'|'away': {'pitching': [...], 'batting': [...]}}."""
    t = ((data or {}).get("result") or {}).get("textRelayData") or {}
    out = {}
    for side in ("home", "away"):
        lineup, entry = t.get(side + "Lineup") or {}, t.get(side + "Entry") or {}
        pitching = []
        for p in sorted(lineup.get("pitcher") or [], key=lambda p: _int(p.get("seqno"), 99)):
            pitching.append({
                "pitcher_id": str(p.get("pcode")), "pitcher_name": p.get("name"), "hand": _hand(p), "used": 1,
                "order": _int(p.get("seqno")), "starter": int(_int(p.get("seqno")) == 1),
                "outs": innings_to_outs(p.get("inn")), "pitches": _int(p.get("ballCount")),
                **{k: _int(p.get(k)) for k in ("hit", "bb", "hbp", "kk", "hr", "run", "er")},
            })
        used = {p["pitcher_id"] for p in pitching}
        for p in entry.get("pitcher") or []:
            if str(p.get("pcode")) not in used:
                pitching.append({"pitcher_id": str(p.get("pcode")), "pitcher_name": p.get("name"), "hand": _hand(p),
                                 "used": 0, "order": "", "starter": 0, "outs": 0, "pitches": 0,
                                 **{k: 0 for k in ("hit", "bb", "hbp", "kk", "hr", "run", "er")}})
        batting = [{"bat_order": _int(b.get("batOrder")), "seqno": _int(b.get("seqno")), "batter_id": str(b.get("pcode")),
                    "batter_name": b.get("name"), "pos": b.get("posName"),
                    **{k: _int(b.get(k)) for k in ("pa", "ab", "hit", "hr", "bb", "so", "rbi", "run")}}
                   for b in lineup.get("batter") or []]
        batting.sort(key=lambda b: (b["bat_order"], b["seqno"]))
        out[side] = {"pitching": pitching, "batting": batting}
    return out


def last_inning_file(cache, game_id):
    files = sorted(Path(cache, game_id).glob("inning_*.json"))
    return files[-1] if files else None


def game_meta(paths):
    """투구 CSV -> 경기 정보와 (경기, 투수)별 주 직구 계열 구속."""
    games, speeds, fb_counts = {}, defaultdict(list), defaultdict(Counter)
    rows = []
    for path in paths:
        with open(path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                games.setdefault(r["game_id"], {k: r[k] for k in ("game_date", "round_code", "away_team", "home_team")})
                if r["pitch_type"] in FASTBALLS:
                    fb_counts[r["pitcher_id"]][r["pitch_type"]] += 1
                    rows.append((r["game_id"], r["pitcher_id"], r["pitch_type"], r["speed_kmh"]))
    primary = {p: c.most_common(1)[0][0] for p, c in fb_counts.items()}
    for gid, pid, t, v in rows:
        if t == primary[pid] and v not in ("", None):
            speeds[(gid, pid)].append(float(v))
    return games, speeds


def build(paths, cache="data/raw"):
    games, speeds = game_meta(paths)
    pitching, batting, missing = [], [], []
    for gid, g in sorted(games.items(), key=lambda kv: (kv[1]["game_date"], kv[0])):
        f = last_inning_file(cache, gid)
        if f is None:
            missing.append(gid)
            continue
        box = parse_box(json.loads(f.read_text(encoding="utf-8")))
        for side in ("away", "home"):
            team = g[side + "_team"]
            opp = g["home_team" if side == "away" else "away_team"]
            base = {"game_date": g["game_date"], "round_code": g["round_code"], "game_id": gid, "team": team,
                    "opponent": opp, "home": int(side == "home")}
            for p in box[side]["pitching"]:
                v = speeds.get((gid, p["pitcher_id"]), [])
                pitching.append({**base, **p, "fb_kmh": round(sum(v) / len(v), 1) if v else "", "fb_n": len(v)})
            for b in box[side]["batting"]:
                batting.append({**base, **b})
    return pitching, batting, missing


def write_csv(rows, columns, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description="문자중계 캐시 -> 경기 기록·엔트리 CSV")
    ap.add_argument("csv", nargs="+", help="투구 CSV 경로 (glob 가능)")
    ap.add_argument("--cache", default="data/raw")
    ap.add_argument("--out", default="datasets/kbo_games")
    args = ap.parse_args(argv)
    paths = sorted({p for pattern in args.csv for p in glob.glob(pattern)})
    pitching, batting, missing = build(paths, args.cache)
    out = Path(args.out)
    write_csv(pitching, PITCHING_COLUMNS, out / "pitching.csv")
    write_csv(batting, BATTING_COLUMNS, out / "batting.csv")
    used = sum(p["used"] for p in pitching)
    print(f"경기 {len({p['game_id'] for p in pitching})}개: 투수 기록 {used}줄 + 미등판 엔트리 {len(pitching) - used}줄, "
          f"타자 기록 {len(batting)}줄 -> {out}/")
    if missing:
        print(f"원본 캐시가 없는 경기 {len(missing)}개: {', '.join(missing[:5])}{' ...' if len(missing) > 5 else ''}")


if __name__ == "__main__":
    main()
