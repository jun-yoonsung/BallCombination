// 실제 타석(투수·타자·타석 그대로)을 한 번씩 시뮬레이션해서 리그 결과 비율을 실제와 비교한다.
// tests/test_simulate.py가 실행한다: node tests/sim_calibration.js
const fs = require("fs");
const path = require("path");
const web = path.join(__dirname, "..", "web");
const BP = require(path.join(web, "predict.js"));
const BS = require(path.join(web, "simulate.js"));
const d = JSON.parse(fs.readFileSync(path.join(web, "pitches.json"), "utf8"));
const c = d.cols;
const ctx = { N: d.meta.count, P: c.p, B: c.b, T: c.t, S: c.s, PV: c.pv, ZN: c.zn, RV: c.rv, R: c.r,
  hands: d.pitchers.map((p) => p[2]), nTypes: d.types.length, model: d.model, results: d.results, paResults: d.pa_results };
const sim = BS.create(ctx, BP.create(ctx));
const SAME = { "병살타": "땅볼", "희생번트": "땅볼", "번트아웃": "땅볼", "야수선택": "땅볼", "기타": "땅볼",
  "희생플라이": "플라이", "라인드라이브": "직선타", "실책출루": "실책", "고의4구": "볼넷" };
const actual = {}, simulated = {}, perPitcher = new Map();
const rng = BS.mulberry32(7);
const REPS = 3;
let n = 0, simPitches = 0, pas = 0;
for (let i = 0; i < ctx.N; i++) {
  if (c.r[i] == null || !(c.r[i] & 1)) continue;
  pas++;
  const pa = d.pa_results[c.r[i] >> 1];
  const res = SAME[pa] || pa;
  actual[res] = (actual[res] || 0) + 1;
  n++;
  let e = perPitcher.get(c.p[i]);
  if (!e) perPitcher.set(c.p[i], (e = { n: 0, k: 0, bb: 0, sn: 0, sk: 0, sbb: 0 }));
  e.n++; e.k += res === "삼진"; e.bb += res === "볼넷" || res === "사구";
  // 타석마다 3번 돌려 시뮬레이션 쪽 우연한 흔들림을 줄인다
  for (let rep = 0; rep < REPS; rep++) {
    const r = sim.simulatePA({ pitcher: c.p[i], batter: c.b[i], stance: (c.s[i] >> 9) & 1 }, rng);
    simulated[r.result] = (simulated[r.result] || 0) + 1;
    simPitches += r.pitches.length;
    e.sn++; e.sk += r.result === "삼진"; e.sbb += r.result === "볼넷" || r.result === "사구";
  }
}
// 300타석 이상 투수의 삼진율·볼넷율: 시뮬레이션을 실제에 회귀한 기울기와 상관계수
function fit(xs, ys) {
  const mx = xs.reduce((a, v) => a + v) / xs.length, my = ys.reduce((a, v) => a + v) / ys.length;
  let sxy = 0, sxx = 0, syy = 0;
  xs.forEach((x, k) => { sxy += (x - mx) * (ys[k] - my); sxx += (x - mx) ** 2; syy += (ys[k] - my) ** 2; });
  return { slope: sxy / sxx, r: sxy / Math.sqrt(sxx * syy) };
}
const big = [...perPitcher.values()].filter((e) => e.n >= 300);
const perPitcherFit = {
  pitchers: big.length,
  k: fit(big.map((e) => e.k / e.n), big.map((e) => e.sk / e.sn)),
  bb: fit(big.map((e) => e.bb / e.n), big.map((e) => e.sbb / e.sn)),
};
const rate = (o, total) => Object.fromEntries(Object.entries(o).map(([k, v]) => [k, v / total]));
console.log(JSON.stringify({ n, actual: rate(actual, n), simulated: rate(simulated, n * REPS),
  pitches_per_pa: { actual: ctx.N / pas, simulated: simPitches / (n * REPS) }, per_pitcher: perPitcherFit }));
