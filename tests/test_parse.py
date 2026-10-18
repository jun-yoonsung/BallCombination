import json
import unittest
from pathlib import Path

from kbo_crawler.crawl import crawl_game
from kbo_crawler.parse import parse_games, parse_relay, plate_z

# 2025-09-20 한화-KT 1회 실제 응답 (용량 때문에 선수 상세 기록 필드는 제거)
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "relay_20250920HHKT02025_inning_01.json").read_text(encoding="utf-8")
)


class ParseRelayTest(unittest.TestCase):
    def setUp(self):
        self.rows = parse_relay(FIXTURE, "20250920HHKT02025", 1)

    def test_extracts_all_pitches_in_order(self):
        self.assertEqual(len(self.rows), 48)
        seqnos = [r["seqno"] for r in self.rows]
        self.assertEqual(seqnos, sorted(seqnos))
        self.assertTrue(all(r["pitch_type"] and r["speed_kmh"] for r in self.rows))

    def test_first_pitch(self):
        first = self.rows[0]
        self.assertEqual(first["half"], "top")
        self.assertEqual((first["pitcher_name"], first["batter_name"]), ("고영표", "황영묵"))
        self.assertEqual((first["pitch_type"], first["speed_kmh"], first["pitch_result"]), ("투심", 134, "S"))
        self.assertEqual((first["balls"], first["strikes"], first["outs"]), (0, 1, 0))
        self.assertEqual(first["plate_x"], -0.5331)
        self.assertAlmostEqual(first["plate_z"], 1.0893, places=3)

    def test_last_pitch_is_bottom_half(self):
        last = self.rows[-1]
        self.assertEqual((last["half"], last["pitcher_name"], last["batter_name"]), ("bottom", "폰세", "장준원"))

    def test_plate_z_within_physical_range(self):
        self.assertTrue(all(-1 < r["plate_z"] < 7 for r in self.rows))

    def test_plate_z_missing_trajectory(self):
        self.assertIsNone(plate_z({"crossPlateX": 0.1}))

    def test_empty_response(self):
        self.assertEqual(parse_relay({}, "G1", 1), [])
        self.assertEqual(parse_relay({"result": {"textRelayData": None}}, "G1", 1), [])


class ParseGamesTest(unittest.TestCase):
    def test_filters_unfinished_and_cancelled(self):
        data = {"result": {"games": [
            {"gameId": "A", "categoryId": "kbo", "statusCode": "RESULT", "roundCode": "kbo_r"},
            {"gameId": "B", "categoryId": "kbo", "statusCode": "BEFORE", "roundCode": "kbo_r"},
            {"gameId": "C", "categoryId": "kbo", "statusCode": "RESULT", "cancel": True, "roundCode": "kbo_r"},
            {"gameId": "D", "categoryId": "futures", "statusCode": "RESULT", "roundCode": "kbo_r"},
            {"gameId": "E", "categoryId": "kbo", "statusCode": "RESULT", "roundCode": "kbo_e"},
            {"gameId": "F", "categoryId": "kbo", "statusCode": "RESULT", "roundCode": "kbo_ps_wd"},
            {"gameId": "G", "categoryId": "kbo", "statusCode": "RESULT", "roundCode": "kbo_as"},
        ]}}
        self.assertEqual([g["game_id"] for g in parse_games(data)], ["A", "F"])
        self.assertEqual([g["game_id"] for g in parse_games(data, official_only=False)], ["A", "E", "F", "G"])


class FakeClient:
    """9회까지만 데이터가 있는 경기를 흉내낸다."""

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
        rows = crawl_game(client, "G1", {"game_date": "2025-09-20", "home_team": "KT", "away_team": "한화"})
        self.assertEqual(client.calls, list(range(1, 11)))
        self.assertEqual(len(rows), 48 * 9)
        self.assertEqual(rows[0]["home_team"], "KT")


if __name__ == "__main__":
    unittest.main()
