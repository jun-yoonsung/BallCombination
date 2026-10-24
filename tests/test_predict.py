import json
import shutil
import subprocess
import unittest
from pathlib import Path

from kbo_crawler import predict

ROOT = Path(__file__).resolve().parent.parent
CSVS = sorted((ROOT / "datasets" / "kbo_pitches").glob("2026-*.csv"))
WEB_DATA = ROOT / "web" / "pitches.json"


def row(pitcher="p1", batter="b1", t="직구", b=0, s=0, stance="R", prev="-", zone=4):
    return {"pitcher_id": pitcher, "batter_id": batter, "pitch_type": t, "_cnt": f"{b}{s}", "_st": stance,
            "_prev": prev, "_hand": "R", "_cg": predict.count_group(b, s), "_zone": zone}


class ZoneTest(unittest.TestCase):
    def test_zone_grid(self):
        top, bot = predict.ZONE_TOP, predict.ZONE_BOT
        self.assertEqual(predict.zone_of(0, (top + bot) / 2), 4)  # 한가운데
        self.assertEqual(predict.zone_of(-0.7, top - 0.05), 0)  # 존 안 위·왼쪽(3루 쪽)
        self.assertEqual(predict.zone_of(0.7, bot + 0.05), 8)
        self.assertEqual(predict.zone_of(-1.5, 4.0), 9)  # 바깥 위·왼
        self.assertEqual(predict.zone_of(1.5, 0.5), 12)  # 바깥 아래·오
        self.assertIsNone(predict.zone_of(None, 2.0))

    def test_count_group(self):
        self.assertEqual([predict.count_group(*c) for c in [(0, 0), (1, 2), (3, 1), (1, 1)]], ["first", "two", "behind", "other"])


class TypeModelTest(unittest.TestCase):
    def test_backs_off_to_broader_situation(self):
        # 이 투수는 0-0에서 직구만, 그 밖에서는 슬라이더만 던졌다
        rows = [row(t="직구") for _ in range(30)] + [row(t="슬라이더", b=1, s=2) for _ in range(30)]
        m = predict.TypeModel(beta=1e9).fit(rows)
        first = m.predict(row())
        self.assertGreater(first["직구"], 0.7)
        # 한 번도 없던 카운트는 투수 전체 비율(반반)에 가깝다
        unseen = m.predict(row(b=3, s=0))
        self.assertAlmostEqual(unseen["직구"], 0.5, delta=0.05)
        self.assertAlmostEqual(sum(unseen.values()), 1.0)


@unittest.skipUnless(CSVS and WEB_DATA.exists() and shutil.which("node"), "데이터와 node가 있어야 함")
class WebMatchesPythonTest(unittest.TestCase):
    """web/predict.js가 파이썬 모델과 같은 확률을 내는지 실제 시즌 데이터로 확인한다."""

    @classmethod
    def setUpClass(cls):
        rows = predict.annotate(predict.load_rows(CSVS))
        cls.type_model = predict.TypeModel().fit(rows)
        cls.zone_model = predict.ZoneModel().fit(rows)
        # 투구 수 상위 투수 몇 명의 실제 상황을 질의로 쓴다
        cls.queries = [r for r in rows if r["pitch_num"] not in ("", "1")][::40000][:6]

    def test_type_and_zone_probabilities_match(self):
        data = json.loads(WEB_DATA.read_text(encoding="utf-8"))
        pidx = {}
        bidx = {}
        # export_web의 인덱스는 등장 순서이므로 이름이 아니라 id로 찾아야 한다: CSV를 같은 순서로 다시 읽어 매핑
        for r in predict.annotate(predict.load_rows(CSVS)):
            pidx.setdefault(r["pitcher_id"], len(pidx))
            bidx.setdefault(r["batter_id"], len(bidx))
        types = data["types"]
        qs = [{"pitcher": pidx[r["pitcher_id"]], "batter": bidx[r["batter_id"]], "stance": int(r["_st"] == "L"),
               "balls": int(r["_cnt"][0]), "strikes": int(r["_cnt"][1]),
               "prev": types.index(r["_prev"]) if r["_prev"] != "-" else -1, "type": types.index(r["pitch_type"])} for r in self.queries]
        script = f"""
const fs = require("fs"); const BP = require({json.dumps(str(ROOT / "web" / "predict.js"))});
const d = JSON.parse(fs.readFileSync({json.dumps(str(WEB_DATA))}, "utf8")); const c = d.cols;
const m = BP.create({{ N: d.meta.count, P: c.p, B: c.b, T: c.t, S: c.s, PV: c.pv, ZN: c.zn, RV: c.rv,
  hands: d.pitchers.map((p) => p[2]), nTypes: d.types.length, model: d.model }});
const qs = {json.dumps(qs)};
console.log(JSON.stringify(qs.map((q) => ({{ t: m.typeProbs(q).probs, z: m.zoneProbs(q, q.type).probs }}))));
"""
        out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
        for r, q, o in zip(self.queries, qs, out):
            py = self.type_model.predict(r)
            for k, t in enumerate(types):
                # 타자 보정값은 웹 데이터에 소수 다섯째 자리까지 저장된다
                self.assertAlmostEqual(o["t"][k], py[t], places=4, msg=f"{r['pitcher_name']} {t}")
            pz = self.zone_model.predict(r, r["pitch_type"])
            for z in range(predict.N_ZONES):
                self.assertAlmostEqual(o["z"][z], pz[z], places=6)


if __name__ == "__main__":
    unittest.main()
