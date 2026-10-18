import json
import unittest
from pathlib import Path

from kbo_crawler.crawl import crawl_game
from kbo_crawler.parse import classify_pa, parse_games, parse_relay, plate_z

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
        self.assertEqual((first["balls"], first["strikes"], first["outs"]), (0, 0, 0))
        self.assertEqual(first["plate_x"], -0.5331)
        self.assertAlmostEqual(first["plate_z"], 1.0893, places=3)

    def test_pre_pitch_situation(self):
        by_seq = {r["seqno"]: r for r in self.rows}
        # 3루타 이후 무사 3루, 볼 1개 뒤의 공
        r = by_seq[7]
        self.assertEqual((r["balls"], r["strikes"], r["outs"], r["on_1b"], r["on_2b"], r["on_3b"]), (1, 0, 0, 0, 0, 1))
        # 피치클락 위반 스트라이크 직후, 무사 1·2루
        r = by_seq[40]
        self.assertEqual((r["balls"], r["strikes"], r["on_1b"], r["on_2b"], r["on_3b"]), (0, 1, 1, 1, 0))
        # 3점 홈런 이후: 투수팀(원정 한화) 기준 0-3
        r = by_seq[46]
        self.assertEqual((r["away_score"], r["home_score"], r["score_diff"]), (0, 3, -3))
        # 원정 투수가 던진 1회초에는 홈팀 기준 점수차
        self.assertEqual(by_seq[2]["score_diff"], 0)

    def test_plate_appearance_result(self):
        by_seq = {r["seqno"]: r for r in self.rows}
        # 황영묵 3루타: 2구째가 결정구
        self.assertEqual([(by_seq[s]["pa_result"], by_seq[s]["pa_last_pitch"]) for s in (2, 3)], [("3루타", 0), ("3루타", 1)])
        # 3루주자 태그아웃(주자 이벤트)이 타자 결과를 덮어쓰지 않는다
        self.assertEqual(by_seq[8]["pa_result"], "플라이")
        self.assertEqual(by_seq[40]["pa_result"], "홈런")
        self.assertEqual(by_seq[67]["pa_result"], "삼진")
        self.assertTrue(all(r["pa_result"] for r in self.rows))
        self.assertEqual(sum(r["pa_last_pitch"] for r in self.rows), 10)  # 1회초 3타석 + 1회말 7타석

    def test_classify_pa(self):
        cases = {
            "김타자 : 스트라이크 낫아웃 폭투로 출루": "삼진",
            "김타자 : 유격수 땅볼 실책으로 출루": "실책출루",
            "김타자 : 자동 고의4구": "고의4구",
            "김타자 : 몸에 맞는 볼": "사구",
            "김타자 : 투수 맞고 유격수 내야안타": "1루타",
            "김타자 : 우중간 그라운드 홈런": "홈런",
            "김타자 : 희생번트 야수선택으로 출루": "희생번트",
            "김타자 : 유격수 병살타 아웃": "병살타",
            "김타자 : 2루수 앞 땅볼로 출루": "땅볼",
            "김타자 : 포수 파울플라이 아웃": "플라이",
            "김타자 : 쓰리번트 아웃": "번트아웃",
        }
        for text, expected in cases.items():
            self.assertEqual(classify_pa(text), expected, text)

    def test_last_pitch_is_bottom_half(self):
        last = self.rows[-1]
        self.assertEqual((last["half"], last["pitcher_name"], last["batter_name"]), ("bottom", "폰세", "장준원"))

    def test_plate_z_within_physical_range(self):
        self.assertTrue(all(-1 < r["plate_z"] < 7 for r in self.rows))

    def test_plate_z_missing_trajectory(self):
        self.assertIsNone(plate_z({"crossPlateX": 0.1}))

    def test_duplicate_relay_is_ignored(self):
        data = json.loads(json.dumps(FIXTURE))
        relays = data["result"]["textRelayData"]["textRelays"]
        relays.append(json.loads(json.dumps(relays[0])))
        self.assertEqual(len(parse_relay(data, "G", 1)), len(self.rows))

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
