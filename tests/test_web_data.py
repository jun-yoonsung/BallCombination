import json
import math
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


@unittest.skipUnless((WEB / "pitches.json").exists(), "웹 데이터가 있어야 함")
class WebTrajectoryTest(unittest.TestCase):
    """웹용 궤적 파일로 다시 계산한 홈플레이트 통과 위치가 원본 위치(x)와 맞는지 모든 공을 확인한다.

    2026년 7월 중순 측정 시작 지점(y0)이 50ft에서 55ft로 바뀐 것을 놓쳐 31%의 궤적이 틀어진 적이 있다.
    """

    def test_trajectories_reproduce_plate_location(self):
        data = json.loads((WEB / "pitches.json").read_text(encoding="utf-8"))
        xs = data["cols"]["x"]
        checked = bad = 0
        y0s = set()
        for f in sorted((WEB / "traj").glob("*.json")):
            t = json.loads(f.read_text(encoding="utf-8"))
            for k, i in enumerate(t["i"]):
                if xs[i] is None:
                    continue
                x0, vx0, vy0, ax, ay, y0 = (t[n][k] / 100 for n in ("x0", "vx0", "vy0", "ax", "ay", "y0"))
                y0s.add(y0)
                a, b, c = ay / 2, vy0, y0 - 0.7083  # 네이버 crossPlateX는 y=0.708ft 지점
                tt = (-b - math.sqrt(b * b - 4 * a * c)) / (2 * a)
                x = x0 + vx0 * tt + ax * tt * tt / 2
                checked += 1
                bad += abs(x - xs[i] / 100) > 0.03  # 1/100 단위 반올림 오차 허용
        self.assertGreater(checked, 200000)
        self.assertEqual(bad, 0, f"{bad}/{checked}개 공의 궤적이 위치와 맞지 않음")
        self.assertEqual(y0s, {50.0, 55.0})


if __name__ == "__main__":
    unittest.main()
