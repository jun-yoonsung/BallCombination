import json
import unittest
from pathlib import Path

from kbo_crawler.crawl import crawl_game
from kbo_crawler.parse import parse_games, parse_relay

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "relay_inning_01.json").read_text(encoding="utf-8"))


class ParseRelayTest(unittest.TestCase):
    def test_extracts_pitches_in_order(self):
        rows = parse_relay(FIXTURE, "G1", 1)
        self.assertEqual([r["seqno"] for r in rows], [2, 3, 11])
        first = rows[0]
        self.assertEqual(first["pitch_type"], "직구")
        self.assertEqual(first["speed_kmh"], 148)
        self.assertEqual(first["plate_x"], 0.81)
        self.assertEqual(first["plate_z"], 2.75)
        self.assertEqual(first["half"], "top")
        self.assertEqual(first["pitcher_name"], "최투수")
        self.assertEqual(first["batter_name"], "이타자")
        self.assertEqual(first["stance"], "R")

    def test_full_trajectory_fields(self):
        last = parse_relay(FIXTURE, "G1", 1)[-1]
        self.assertEqual(last["half"], "bottom")
        self.assertEqual((last["vy0"], last["az"]), (-120.3, -30.5))

    def test_empty_response(self):
        self.assertEqual(parse_relay({}, "G1", 1), [])
        self.assertEqual(parse_relay({"result": {"textRelayData": None}}, "G1", 1), [])


class ParseGamesTest(unittest.TestCase):
    def test_filters_unfinished_and_cancelled(self):
        data = {"result": {"games": [
            {"gameId": "A", "categoryId": "kbo", "statusCode": "RESULT"},
            {"gameId": "B", "categoryId": "kbo", "statusCode": "BEFORE"},
            {"gameId": "C", "categoryId": "kbo", "statusCode": "RESULT", "cancel": True},
            {"gameId": "D", "categoryId": "futures", "statusCode": "RESULT"},
        ]}}
        self.assertEqual([g["game_id"] for g in parse_games(data)], ["A"])


class FakeClient:
    """1~10회 중 9회까지만 데이터가 있는 경기를 흉내낸다."""

    def __init__(self):
        self.calls = []

    def relay(self, game_id, inning):
        self.calls.append(inning)
        if inning > 9:
            return {"result": {"textRelayData": {"textRelays": []}}}
        data = json.loads(json.dumps(FIXTURE))
        for r in data["result"]["textRelayData"]["textRelays"]:
            r["inn"] = inning
        return data


class CrawlGameTest(unittest.TestCase):
    def test_stops_after_regulation(self):
        client = FakeClient()
        rows = crawl_game(client, "G1", {"game_date": "2025-09-20", "home_team": "삼성", "away_team": "LG"})
        self.assertEqual(client.calls, list(range(1, 11)))
        self.assertEqual(len(rows), 27)
        self.assertEqual(rows[0]["home_team"], "삼성")


if __name__ == "__main__":
    unittest.main()
