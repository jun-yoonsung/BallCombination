import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless((ROOT / "web" / "pitches.json").exists() and shutil.which("node"), "데이터와 node가 있어야 함")
class SimulationCalibrationTest(unittest.TestCase):
    """시뮬레이션한 타석 결과 비율이 2026 실제 기록과 가까운지 확인한다."""

    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", str(ROOT / "tests" / "sim_calibration.js")], capture_output=True, text=True, check=True)
        cls.r = json.loads(out.stdout)

    def assertRateClose(self, key, tol):
        a, s = self.r["actual"].get(key, 0), self.r["simulated"].get(key, 0)
        self.assertAlmostEqual(s, a, delta=tol, msg=f"{key}: 실제 {a:.3f} / 시뮬레이션 {s:.3f}")

    def test_outcome_rates(self):
        self.assertGreater(self.r["n"], 50000)
        for key, tol in [("삼진", 0.02), ("볼넷", 0.015), ("1루타", 0.015), ("2루타", 0.008), ("홈런", 0.006),
                         ("땅볼", 0.02), ("플라이", 0.02), ("사구", 0.005)]:
            self.assertRateClose(key, tol)

    def test_pitcher_differences_survive(self):
        # 투수마다 다른 삼진율·볼넷율이 평균으로 뭉개지지 않아야 한다 (한 시즌 기록의 우연은 조금 걸러지도록 1 미만)
        f = self.r["per_pitcher"]
        self.assertGreater(f["pitchers"], 40)
        for key in ("k", "bb"):
            self.assertGreater(f[key]["r"], 0.85, key)
            self.assertTrue(0.65 < f[key]["slope"] < 1.1, f"{key} 기울기 {f[key]['slope']:.2f}")

    def test_pitches_per_plate_appearance(self):
        p = self.r["pitches_per_pa"]
        self.assertAlmostEqual(p["simulated"], p["actual"], delta=0.15)


if __name__ == "__main__":
    unittest.main()
