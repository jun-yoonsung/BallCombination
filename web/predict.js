/* 볼배합 예측 — kbo_crawler/predict.py와 같은 계산을 브라우저에서 한다.
 *
 * 구종: 리그 -> 투수 -> +카운트 -> +타자 타석 -> +직전 구종 순으로, 같은 상황의 과거 투구 비율을 쓰되
 *       기록이 적으면 한 단계 넓은 상황 쪽으로 당긴다(알파만큼). 마지막에 타자별 보정 비율을 곱한다.
 * 코스: 리그 (구종, 투수 손, 타석) -> +카운트 상황, 여기에 투수 개인 (구종, 타석) 경향을 곱한다.
 * 가치: 공 하나의 가치(rv, 타자 기준 득점)를 리그 (구종, 카운트) -> 투수 구종 -> +카운트 -> +타석 순으로 수축 평균.
 *
 * 상수는 pitches.json의 model 항목에서 받으므로 파이썬과 따로 맞출 필요가 없다.
 */
(function (root) {
  "use strict";
  const N_ZONES = 13;

  function countGroup(b, s) {
    if (b === 0 && s === 0) return 0;  // first
    if (s === 2) return 1;              // two
    return b > s ? 2 : 3;               // behind / other
  }
  const smooth = (counts, n, prior, a) => prior.map((p, k) => ((counts ? counts[k] : 0) + a * p) / (n + a));

  /* ctx: { N, P, B, T, S, PV, ZN, RV, hands: ["L"|"R"...] (투수 인덱스별), nTypes, model } */
  function create(ctx) {
    const { N, P, T, S, PV, ZN, RV, nTypes, model } = ctx;
    const balls = (s) => s & 3, strikes = (s) => (s >> 2) & 3, lefty = (s) => (s >> 9) & 1;
    const handOf = (p) => (ctx.hands[p] === "L" ? 1 : 0);

    // 리그 구종 비율, 투수별 투구 목록
    const league = new Float64Array(nTypes);
    const byPitcher = new Map();
    for (let i = 0; i < N; i++) {
      league[T[i]]++;
      let arr = byPitcher.get(P[i]);
      if (!arr) byPitcher.set(P[i], (arr = []));
      arr.push(i);
    }
    for (let k = 0; k < nTypes; k++) league[k] /= N;

    // 코스 리그 표: l1[type][hand][stance], l2[...][cg], 그리고 리그 전체 존 분포
    const L1 = new Float64Array(nTypes * 4 * N_ZONES), L2 = new Float64Array(nTypes * 16 * N_ZONES);
    const zoneLeague = new Float64Array(N_ZONES);
    // 가치 리그 표: (구종, 카운트)의 rv 합과 개수
    const vSum = new Float64Array(nTypes * 12), vN = new Float64Array(nTypes * 12);
    let zN = 0;
    for (let i = 0; i < N; i++) {
      const s = S[i], t = T[i];
      if (ZN[i] != null) {
        const h = handOf(P[i]), st = lefty(s), z = ZN[i];
        L1[((t * 2 + h) * 2 + st) * N_ZONES + z]++;
        L2[(((t * 2 + h) * 2 + st) * 4 + countGroup(balls(s), strikes(s))) * N_ZONES + z]++;
        zoneLeague[z]++; zN++;
      }
      if (RV[i] != null) { const c = t * 12 + balls(s) * 3 + strikes(s); vSum[c] += RV[i] / 1000; vN[c]++; }
    }
    for (let z = 0; z < N_ZONES; z++) zoneLeague[z] /= zN;
    const slice = (arr, off) => { const c = Array.from(arr.subarray(off, off + N_ZONES)); return [c, c.reduce((a, v) => a + v, 0)]; };

    /* q: { pitcher, batter(-1 = 지정 안 함), stance(0 우타 / 1 좌타), balls, strikes, prev(-1 = 초구) } */
    function typeProbs(q) {
      const pitches = byPitcher.get(q.pitcher) || [];
      const levels = [new Float64Array(nTypes), new Float64Array(nTypes), new Float64Array(nTypes), new Float64Array(nTypes)];
      const n = [0, 0, 0, 0];
      for (const i of pitches) {
        const s = S[i], t = T[i];
        levels[0][t]++; n[0]++;
        if (balls(s) !== q.balls || strikes(s) !== q.strikes) continue;
        levels[1][t]++; n[1]++;
        if (lefty(s) !== q.stance) continue;
        levels[2][t]++; n[2]++;
        if ((PV[i] == null ? -1 : PV[i]) !== q.prev) continue;
        levels[3][t]++; n[3]++;
      }
      let p = Array.from(league);
      model.type_alphas.forEach((a, k) => { p = smooth(levels[k], n[k], p, a); });
      const ratio = q.batter >= 0 ? model.batter_ratio[q.batter] : null;
      if (ratio) {
        p = p.map((v, k) => v * ratio[k]);
        const sum = p.reduce((a, v) => a + v, 0);
        p = p.map((v) => v / sum);
      }
      return { probs: p, evidence: n, batterAdjusted: !!ratio };
    }

    function zoneProbs(q, type) {
      const h = handOf(q.pitcher), cg = countGroup(q.balls, q.strikes);
      const [a1, a2] = model.zone_alpha_league;
      const [c1, n1] = slice(L1, ((type * 2 + h) * 2 + q.stance) * N_ZONES);
      const l1 = smooth(c1, n1, Array.from(zoneLeague), a1);
      const [c2, n2] = slice(L2, (((type * 2 + h) * 2 + q.stance) * 4 + cg) * N_ZONES);
      const l2 = smooth(c2, n2, l1, a2);
      const pc = new Array(N_ZONES).fill(0); let pn = 0;
      for (const i of byPitcher.get(q.pitcher) || []) {
        if (T[i] !== type || ZN[i] == null || lefty(S[i]) !== q.stance) continue;
        pc[ZN[i]]++; pn++;
      }
      const pp = smooth(pc, pn, l1, model.zone_alpha_pitcher);
      const out = pp.map((v, z) => v * l2[z] / l1[z]);
      const sum = out.reduce((a, v) => a + v, 0);
      return { probs: out.map((v) => v / sum), n: pn };
    }

    /* 구종별 투구 가치: 타자 기준 득점(공 1개당). 투수에게 좋은 공은 음수. */
    function typeValues(q) {
      const a = model.value_alpha, c = q.balls * 3 + q.strikes;
      const lv = (t) => (vN[t * 12 + c] ? vSum[t * 12 + c] / vN[t * 12 + c] : 0);
      const acc = Array.from({ length: nTypes }, () => [[0, 0], [0, 0], [0, 0]]);  // [투수 구종], [+카운트], [+타석]
      const whiff = Array.from({ length: nTypes }, () => [0, 0]);
      for (const i of byPitcher.get(q.pitcher) || []) {
        const s = S[i], t = T[i];
        if (RV[i] == null) continue;
        const v = RV[i] / 1000;
        acc[t][0][0] += v; acc[t][0][1]++;
        if (balls(s) !== q.balls || strikes(s) !== q.strikes) continue;
        acc[t][1][0] += v; acc[t][1][1]++;
        if (lefty(s) !== q.stance) continue;
        acc[t][2][0] += v; acc[t][2][1]++;
      }
      return acc.map((lv3, t) => {
        let m = lv(t);
        for (const [sum, n] of lv3) m = (sum + a * m) / (n + a);
        return { type: t, value: m, n: lv3[1][1], nStance: lv3[2][1], nTotal: lv3[0][1] };
      });
    }

    return { typeProbs, zoneProbs, typeValues, league: Array.from(league), countGroup };
  }

  const api = { create, N_ZONES, countGroup };
  root.BallPredict = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
