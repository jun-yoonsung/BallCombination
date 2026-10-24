// 게임 엔진 규칙 확인용. tests/test_simulate.py가 실행한다: node tests/game_check.js
const fs = require("fs");
const path = require("path");
const web = path.join(__dirname, "..", "web");
const BP = require(path.join(web, "predict.js"));
const BS = require(path.join(web, "simulate.js"));
const d = JSON.parse(fs.readFileSync(path.join(web, "pitches.json"), "utf8"));
const c = d.cols;
const ctx = { N: d.meta.count, P: c.p, B: c.b, T: c.t, S: c.s, PV: c.pv, ZN: c.zn, RV: c.rv, R: c.r, X: c.x, Z: c.z,
  hands: d.pitchers.map((p) => p[2]), nTypes: d.types.length, model: d.model, results: d.results, paResults: d.pa_results };
const sim = BS.create(ctx, BP.create(ctx));
const rng = BS.mulberry32(11);
// 가장 많이 던진 투수의 주 구종
const counts = new Map(); for (let i = 0; i < ctx.N; i++) counts.set(c.p[i], (counts.get(c.p[i]) || 0) + 1);
const pitcher = [...counts].sort((a, b) => b[1] - a[1])[0][0];
const m = { pitcher, batter: -1, stance: 0 };
// 노린 곳(존 아래 가운데) vs 무작위로 고른 이 투수의 공: 노린 곳까지 평균 거리
const target = [0, 1.9];
let aimed = 0, random = 0;
const pool = sim.pitchesOf(pitcher, 0).idx;
for (let k = 0; k < 2000; k++) {
  const a = sim.aimPitch(pitcher, 0, target[0], target[1], rng);
  aimed += Math.hypot(a.x - target[0], a.z - target[1]);
  const i = pool[Math.floor(rng() * pool.length)];
  random += Math.hypot(c.x[i] / 100 - target[0], c.z[i] / 100 - target[1]);
}
const swings = (sw) => { const r = {}; for (let k = 0; k < 500; k++) { const x = sim.swingResult(m, 1, 1, 0, 4, sw, rng).res; r[x] = (r[x] || 0) + 1; } return r; };
console.log(JSON.stringify({
  called: { middle: sim.calledStrikeProb(4, 1, 1), outsideLow: sim.calledStrikeProb(11, 1, 1) },
  aim: { aimed: aimed / 2000, random: random / 2000 },
  perfect: swings({ dt: 0, dx: 0, dz: 0 }), late: swings({ dt: 0.2, dx: 0, dz: 0 }), take: swings(null),
}));
