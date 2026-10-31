import csv
import json
import unittest
from collections import Counter
from pathlib import Path

from kbo_crawler import boxscore, workload

ROOT = Path(__file__).resolve().parent.parent
PITCHING = ROOT / "datasets" / "kbo_games" / "pitching.csv"
WEB = ROOT / "web" / "workload.json"


class BoxScoreTest(unittest.TestCase):
    def test_innings_to_outs(self):
        self.assertEqual([boxscore.innings_to_outs(v) for v in ("7.0", "0.1", "6.2", "", None)], [21, 1, 20, 0, 0])

    def test_parse_box_merges_lineup_and_unused_entry(self):
        data = {"result": {"textRelayData": {
            "homeLineup": {"pitcher": [
                {"pcode": "2", "name": "불펜", "seqno": 2, "inn": "1.0", "ballCount": 18, "er": 1, "hitType": "좌투좌타"},
                {"pcode": "1", "name": "선발", "seqno": 1, "inn": "6.1", "ballCount": 97, "kk": 7, "hitType": "우투우타"}],
                "batter": [{"pcode": "9", "name": "타자", "batOrder": 1, "seqno": 1, "pa": 4, "hit": 2}]},
            "homeEntry": {"pitcher": [{"pcode": "1", "name": "선발"}, {"pcode": "3", "name": "휴식", "pitchingStyle": "좌완투수"}]},
        }}}
        home = boxscore.parse_box(data)["home"]
        p = {x["pitcher_name"]: x for x in home["pitching"]}
        self.assertEqual([x["pitcher_name"] for x in home["pitching"]], ["선발", "불펜", "휴식"])
        self.assertEqual((p["선발"]["starter"], p["선발"]["outs"], p["선발"]["pitches"], p["선발"]["kk"]), (1, 19, 97, 7))
        self.assertEqual((p["불펜"]["hand"], p["휴식"]["hand"], p["휴식"]["used"]), ("L", "L", 0))
        self.assertEqual(home["batting"][0]["hit"], 2)


def _season(apps, team_days):
    """apps: [(date, pid, pitches, start)], team_days: 엔트리가 있던 날짜들 (투수 p1~p3, 엔트리는 12명으로 채움)."""
    rows = []
    filler = [f"f{k}" for k in range(10)]
    used = {(d, pid): (n, st) for d, pid, n, st in apps}
    for d in team_days:
        for pid in ["p1", "p2", "p3", *filler]:
            n, st = used.get((d, pid), (0, 0))
            rows.append({"game_date": d, "round_code": "kbo_r", "game_id": f"g{d}", "team": "A", "opponent": "B",
                         "home": "1", "pitcher_id": pid, "pitcher_name": pid, "hand": "R", "used": int((d, pid) in used),
                         "order": "", "starter": st, "outs": 3, "pitches": n, "hit": 0, "bb": 0, "hbp": 0, "kk": 0,
                         "hr": 0, "run": 0, "er": 0, "fb_kmh": "", "fb_n": 0})
    return workload.Season(rows)


class StateTest(unittest.TestCase):
    def test_streak_and_recent_load(self):
        days = [f"2026-05-{d:02d}" for d in range(1, 11)]
        s = _season([(days[6], "p1", 15, 0), (days[7], "p1", 22, 0), (days[8], "p1", 10, 0), (days[2], "p2", 95, 1)], days)
        st = workload.state(s, "p1", workload.day(days[9]))
        self.assertEqual((st["streak"], st["y"][:3], st["p7"], st["rest"]), (3, [10, 22, 15], 47, 1))
        self.assertFalse(st["starter"])
        sp = workload.state(s, "p2", workload.day(days[9]))
        self.assertTrue(sp["starter"])
        self.assertEqual(sp["since_start"], 7)

    def test_velo_drop_needs_enough_pitches(self):
        apps = {1: {"fb": [(145.0, 20)]}, 2: {"fb": [(145.0, 20)]}, 3: {"fb": [(142.0, 10)]}}
        self.assertAlmostEqual(workload.velo_drop(apps, [1, 2, 3]), -3.0)
        self.assertIsNone(workload.velo_drop(apps, [2, 3]))  # 기준 구속 표본 20개 < 30

    def test_logistic_recovers_direction(self):
        X = [[1.0, x / 10] for x in range(100)]
        y = [int(x >= 50) for x in range(100)]
        w = workload.fit_logistic(X, y, l2=0.1)
        self.assertGreater(w[1], 0)
        self.assertAlmostEqual(workload.auc([workload.predict(w, x) for x in X], y), 1.0)


@unittest.skipUnless(PITCHING.exists(), "경기 기록 CSV가 있어야 함")
class SeasonModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = workload.Workload(workload.Season.load(PITCHING))

    def test_box_pitch_counts_match_pitch_data(self):
        counts = Counter()
        for f in sorted((ROOT / "datasets" / "kbo_pitches").glob("2026-*.csv")):
            with open(f, encoding="utf-8-sig") as fh:
                for r in csv.DictReader(fh):
                    counts[(r["game_id"], r["pitcher_id"])] += 1
        with open(PITCHING, encoding="utf-8-sig") as fh:
            used = [r for r in csv.DictReader(fh) if r["used"] == "1"]
        self.assertEqual(len(used), len(counts))
        off = [r for r in used if int(r["pitches"]) != counts[(r["game_id"], r["pitcher_id"])]]
        self.assertLessEqual(len(off), 5)  # 중계 원본 중복·누락 몇 건은 박스스코어 투구 수를 그대로 쓴다

    def test_workload_rules_learned_from_usage(self):
        w = dict(zip(workload.USAGE_FEATURES, self.m.usage_w))
        self.assertLess(w["2일 연투 뒤"], -1)  # 2연투 뒤에는 거의 쓰지 않는다
        self.assertLess(w["전날 20구 초과분/10"], 0)
        self.assertGreater(w["평소 등판 비율(logit)"], 0.5)
        self.assertGreater(self.m.eval["usage"]["auc"], self.m.eval["usage"]["auc_base"])
        self.assertGreater(self.m.eval["starter"]["top2"], 0.7)

    def test_velocity_drop_raises_drop_risk(self):
        by = self.m.eval["drop"]["by_velo"]
        self.assertGreater(by["-3 이하"][0], 2 * by["-1 초과"][0])

    def test_assess_verdicts(self):
        d = self.m.season.days[-1]
        for team in self.m.season.teams:
            for a in self.m.team_report(team, d)["players"]:
                s = a["state"]
                if a["role"] == "불펜" and s["streak"] >= 3:
                    self.assertEqual(a["verdict"], 2)
                if a["role"] == "선발" and s["since_start"] is not None and s["since_start"] <= 3:
                    self.assertEqual(a["verdict"], 2)
                self.assertTrue(0 <= a["fatigue"] <= 100)


@unittest.skipUnless(WEB.exists(), "웹 데이터가 있어야 함")
class WorkloadWebTest(unittest.TestCase):
    def test_schema(self):
        w = json.loads(WEB.read_text(encoding="utf-8"))
        last = w["meta"]["last_game_day"]
        self.assertGreater(max(w["days"]), last)  # 다음 경기 예측일
        n = len(w["pitchers"])
        for d, day in w["days"].items():
            for g in day["games"]:
                self.assertTrue(0 <= g[1] < len(w["teams"]) and 0 <= g[2] < len(w["teams"]))
            for team in day["teams"].values():
                for r in team["players"]:
                    self.assertTrue(0 <= r[0] < n and r[1] in (0, 1, 2) and 0 <= r[2] <= 100 and len(r[9]) == 5)


if __name__ == "__main__":
    unittest.main()
