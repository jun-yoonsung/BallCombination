"""KBO 투구 데이터 수집 CLI.

예)
  python -m kbo_crawler.crawl --from 2025-09-01 --to 2025-09-07
  python -m kbo_crawler.crawl --game 20250920LGSS02025
"""

import argparse
import csv
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from .client import NaverSportsClient
from .parse import PITCH_COLUMNS, parse_games, parse_relay, player_names

log = logging.getLogger("kbo_crawler")

MAX_INNING = 15
META_COLUMNS = ["game_date", "round_code", "stadium", "away_team", "home_team"]


def crawl_game(client, game_id, game_meta=None):
    rows, names = [], {}
    for inning in range(1, MAX_INNING + 1):
        data = client.relay(game_id, inning)
        names.update(player_names(data))
        # 범위를 벗어난 이닝을 요청하면 다른 이닝 데이터가 올 수 있으므로 걸러낸다.
        inning_rows = [r for r in parse_relay(data, game_id, inning, names)
                       if str(r["inning"]) == str(inning)]
        if not inning_rows and inning >= 9:
            break  # 연장 없음
        rows.extend(inning_rows)
    # 이름은 뒷 이닝 응답에만 있을 수 있으므로 마지막에 한 번 더 채운다.
    for r in rows:
        r["pitcher_name"] = r["pitcher_name"] or names.get(str(r["pitcher_id"]))
        r["batter_name"] = r["batter_name"] or names.get(str(r["batter_id"]))
        if game_meta:
            r.update({k: game_meta.get(k) for k in META_COLUMNS})
    return rows


def daterange_chunks(start, end, days=7):
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=days - 1), end)
        yield cur, stop
        cur = stop + timedelta(days=1)


def main(argv=None):
    ap = argparse.ArgumentParser(description="네이버 스포츠 KBO 투구 데이터 크롤러")
    ap.add_argument("--from", dest="start", type=date.fromisoformat, help="시작일 YYYY-MM-DD")
    ap.add_argument("--to", dest="end", type=date.fromisoformat, help="종료일 YYYY-MM-DD (기본: 시작일)")
    ap.add_argument("--game", action="append", default=[], help="gameId 직접 지정 (여러 번 가능)")
    ap.add_argument("--out", default="data/pitches.csv", help="출력 CSV 경로")
    ap.add_argument("--cache", default="data/raw", help="원본 JSON 캐시 디렉터리 ('' 이면 캐시 안 함)")
    ap.add_argument("--delay", type=float, default=0.7, help="요청 간 최소 간격(초)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if not args.start and not args.game:
        ap.error("--from 또는 --game 중 하나는 필요합니다")

    client = NaverSportsClient(cache_dir=args.cache or None, delay=args.delay)

    games = [{"game_id": g} for g in args.game]
    if args.start:
        end = args.end or args.start
        for s, e in daterange_chunks(args.start, end):
            found = parse_games(client.schedule(s.isoformat(), e.isoformat()))
            log.info("%s ~ %s: 종료된 경기 %d개", s, e, len(found))
            games.extend(found)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=META_COLUMNS + PITCH_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for i, g in enumerate(games, 1):
            try:
                rows = crawl_game(client, g["game_id"], g)
            except Exception as exc:  # 한 경기 실패로 전체를 멈추지 않는다
                log.warning("[%d/%d] %s 실패: %s", i, len(games), g["game_id"], exc)
                continue
            writer.writerows(rows)
            total += len(rows)
            log.info("[%d/%d] %s: 투구 %d개", i, len(games), g["game_id"], len(rows))

    log.info("완료: 경기 %d개, 투구 %d개 -> %s", len(games), total, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
