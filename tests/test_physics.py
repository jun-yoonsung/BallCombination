import json
import unittest
from pathlib import Path

from kbo_crawler.physics import Y_PLATE, movement, position, time_to_y

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "relay_20250920HHKT02025_inning_01.json").read_text(encoding="utf-8")
)
RELAYS = FIXTURE["result"]["textRelayData"]["textRelays"]
PTS = [p for r in RELAYS for p in r.get("ptsOptions") or []]
STUFF = {o["ptsPitchId"]: o["stuff"] for r in RELAYS for o in r["textOptions"] if o.get("ptsPitchId")}


class PhysicsTest(unittest.TestCase):
    def test_position_at_plate_matches_naver(self):
        # 네이버 crossPlateX는 crossPlateY(플레이트 중간, 0.708ft) 지점의 x. 궤적식으로 구한 값과 같아야 한다
        for p in PTS:
            x, y, _ = position(p, time_to_y(p, p["crossPlateY"]))
            self.assertAlmostEqual(x, p["crossPlateX"], places=3)
            self.assertAlmostEqual(y, p["crossPlateY"], places=6)

    def test_flight_time_is_realistic(self):
        for p in PTS:
            self.assertTrue(0.35 < time_to_y(p, Y_PLATE) < 0.55)

    def test_fastball_drops_less_than_curve(self):
        def mean_v(stuff):
            vs = [movement(p)[1] for p in PTS if STUFF.get(p["pitchId"]) == stuff]
            return sum(vs) / len(vs)
        self.assertGreater(mean_v("직구"), 15)  # 회전 덕분에 중력만 받을 때보다 덜 떨어진다
        self.assertLess(mean_v("커브"), 0)  # 탑스핀으로 더 떨어진다

    def test_movement_without_magnus_is_zero(self):
        p = {"x0": 0, "y0": 50, "z0": 6, "vx0": 0, "vy0": -130, "vz0": -5, "ax": 0, "ay": 25, "az": -32.174}
        self.assertEqual(movement(p), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
