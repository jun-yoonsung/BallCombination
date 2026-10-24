/* 타석 시뮬레이션 — 공 하나씩 실제로 진행한다.
 *
 * 매 공마다
 *   1. 구종: predict.js의 구종 확률에서 뽑는다 (볼배합 가정이 있으면 그 구종으로 고정)
 *   2. 코스: predict.js의 코스 확률에서 뽑고, 그 투수가 실제로 그 구종을 그 코스에 던진 공 하나를 골라
 *      구속·위치·궤적을 그대로 쓴다
 *      코스 확률에는 정확한 카운트별 리그 경향과 투수의 존 안 비율(제구)을 곱해 준다
 *   3. 결과: 볼 / 루킹 스트라이크 / 헛스윙 / 파울 / 인플레이 / 사구.
 *      리그의 (코스, 카운트 상황, 구종)별 비율에 타자·투수 성향 비율(존 안/밖 따로)을 곱한다
 *   4. 인플레이면 타구 결과(1루타~홈런, 땅볼, 플라이, 직선타, 실책)를 같은 방식으로 뽑는다
 * 볼넷·삼진·사구·인플레이가 나오면 타석이 끝난다. 주자는 다루지 않는다(병살타는 땅볼로 센다).
 */
(function (root) {
  "use strict";
  const RESULTS = ["볼", "루킹 스트라이크", "헛스윙", "파울", "인플레이", "사구"];
  const BIP = ["1루타", "2루타", "3루타", "홈런", "땅볼", "플라이", "직선타", "실책"];
  // 원본 타석 결과 -> 인플레이 분류
  const BIP_OF = { "1루타": 0, "2루타": 1, "3루타": 2, "홈런": 3, "땅볼": 4, "병살타": 4, "희생번트": 4, "번트아웃": 4, "야수선택": 4, "기타": 4,
    "플라이": 5, "희생플라이": 5, "라인드라이브": 6, "실책출루": 7 };
  // 수축 강도: 결과(공 단위)와 타구(타석 단위)
  // 수축 강도. resBeta·zoneBeta는 투수 59명(300타석 이상)의 삼진율·볼넷율이 실제와 기울기 0.8 안팎으로
  // 맞도록 정했다. 1까지 올리지 않는 것은 한 시즌 기록에 섞인 우연까지 따라가지 않기 위해서다.
  const DEFAULTS = { resAlpha: [20, 50], resBeta: 50, bipAlpha: [30, 60], bipBeta: 80, zoneBeta: 30 };

  function mulberry32(seed) {
    let a = seed >>> 0;
    return function () {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  function pick(probs, r) {
    let acc = 0;
    for (let k = 0; k < probs.length; k++) { acc += probs[k]; if (r < acc) return k; }
    return probs.length - 1;
  }
  const norm = (p) => { const s = p.reduce((a, v) => a + v, 0); return p.map((v) => v / s); };
  const smooth = (c, prior, a) => { const n = c.reduce((x, v) => x + v, 0); return prior.map((p, k) => (c[k] + a * p) / (n + a)); };

  /* ctx: predict.js와 같은 배열 + R(타석 결과*2+결정구), results(결과 코드 이름), paResults(타석 결과 이름), model */
  function create(ctx, PM, opts = {}) {
    const { resAlpha: RES_ALPHA, resBeta: RES_BETA, bipAlpha: BIP_ALPHA, bipBeta: BIP_BETA, zoneBeta: ZONE_BETA } = { ...DEFAULTS, ...opts };
    const { N, P, B, T, S, ZN, R, nTypes } = ctx;
    const balls = (s) => s & 3, strikes = (s) => (s >> 2) & 3, lefty = (s) => (s >> 9) & 1, rcode = (s) => (s >> 10) & 15;
    const NR = RESULTS.length, NB = BIP.length, NZ = 13;
    const cls = new Int8Array(N).fill(-1), bipCls = new Int8Array(N).fill(-1);
    for (let i = 0; i < N; i++) {
      const code = ctx.results[rcode(S[i])], last = R[i] >= 0 && (R[i] & 1), pa = R[i] >= 0 ? ctx.paResults[R[i] >> 1] : null;
      if (code === "B") cls[i] = last && pa === "사구" ? 5 : 0;
      else if (code === "T") cls[i] = 1;
      else if (code === "S" || code === "V") cls[i] = 2;
      else if (code === "F" || code === "W") cls[i] = 3;
      else if (code === "H") { cls[i] = 4; if (last && pa in BIP_OF) bipCls[i] = BIP_OF[pa]; }
    }
    const cg = (b, s) => (b === 0 && s === 0 ? 0 : s === 2 ? 1 : b > s ? 2 : 3);

    // ---- 공 결과: 리그 -> (코스, 카운트 상황) -> (코스, 정확한 카운트) -> +구종
    //      3-0과 2-1처럼 같은 묶음이라도 타자의 스윙 성향이 크게 달라서 정확한 카운트 단계를 둔다
    const r0 = new Array(NR).fill(0), r1 = new Map(), r12 = new Map(), r2 = new Map();
    const bump = (m, k, size, j) => { let a = m.get(k); if (!a) m.set(k, (a = new Array(size).fill(0))); a[j]++; };
    for (let i = 0; i < N; i++) {
      if (cls[i] < 0 || ZN[i] == null) continue;
      const b = balls(S[i]), s = strikes(S[i]), c = cg(b, s), k = ZN[i] * 12 + b * 3 + s;
      r0[cls[i]]++; bump(r1, ZN[i] * 4 + c, NR, cls[i]); bump(r12, k, NR, cls[i]); bump(r2, k * nTypes + T[i], NR, cls[i]);
    }
    const p0 = norm(r0);
    const resCache = new Map();
    const zeros = new Array(NR).fill(0);
    function leagueRes(zone, b, s, type) {
      const k = zone * 12 + b * 3 + s, key = k * nTypes + type;
      let p = resCache.get(key);
      if (!p) {
        p = smooth(r1.get(zone * 4 + cg(b, s)) || zeros, p0, RES_ALPHA[0]);
        p = smooth(r12.get(k) || zeros, p, RES_ALPHA[1]);
        p = smooth(r2.get(key) || zeros, p, RES_ALPHA[1]);
        resCache.set(key, p);
      }
      return p;
    }
    // ---- 코스의 카운트 보정: predict.js 코스 모델은 카운트를 4묶음으로만 본다.
    //      실제로는 3볼에서 존 안 비율이 훨씬 높으므로 (정확한 카운트 / 묶음) 리그 비율을 곱한다
    const zc = Array.from({ length: 12 }, () => new Array(NZ).fill(0)), zg = Array.from({ length: 4 }, () => new Array(NZ).fill(0));
    for (let i = 0; i < N; i++) {
      if (ZN[i] == null) continue;
      const b = balls(S[i]), s = strikes(S[i]);
      zc[b * 3 + s][ZN[i]]++; zg[cg(b, s)][ZN[i]]++;
    }
    const zoneAdj = zc.map((row, k) => {
      const g = norm(zg[cg(Math.floor(k / 3), k % 3)]), c = smooth(row, g, 200);
      return c.map((v, z) => v / g[z]);
    });

    // ---- 타구 결과: 리그 -> 코스 -> +구종
    const b0 = new Array(NB).fill(0), b1 = new Map(), b2 = new Map();
    for (let i = 0; i < N; i++) {
      if (bipCls[i] < 0 || ZN[i] == null) continue;
      b0[bipCls[i]]++; bump(b1, ZN[i], NB, bipCls[i]); bump(b2, ZN[i] * nTypes + T[i], NB, bipCls[i]);
    }
    const q0 = norm(b0);
    function leagueBip(zone, type) {
      let p = smooth(b1.get(zone) || new Array(NB).fill(0), q0, BIP_ALPHA[0]);
      return smooth(b2.get(zone * nTypes + type) || new Array(NB).fill(0), p, BIP_ALPHA[1]);
    }
    // ---- 타자·투수 성향: 실제 / 리그 기대 비율을 beta만큼 1 쪽으로 당긴다 (공 결과는 존 안/밖 따로)
    function ratios(keyOf, size, obsOf, expOf, beta) {
      const obs = new Map(), exp = new Map();
      for (let i = 0; i < N; i++) {
        const o = obsOf(i); if (o < 0) continue;
        const key = keyOf(i), e = expOf(i);
        let ob = obs.get(key), ex = exp.get(key);
        if (!ob) { obs.set(key, (ob = new Array(size).fill(0))); exp.set(key, (ex = new Array(size).fill(0))); }
        ob[o]++; for (let k = 0; k < size; k++) ex[k] += e[k];
      }
      const out = new Map();
      for (const [key, ob] of obs) {
        const ex = exp.get(key), n = ex.reduce((a, v) => a + v, 0);
        out.set(key, ex.map((e, k) => (e ? (ob[k] + beta * e / n) / (e + beta * e / n) : 1)));
      }
      return out;
    }
    const resExp = (i) => leagueRes(ZN[i], balls(S[i]), strikes(S[i]), T[i]);
    const resObs = (i) => (ZN[i] == null ? -1 : cls[i]);
    const inZ = (i) => (ZN[i] < 9 ? 1 : 0);
    const batRes = ratios((i) => B[i] * 2 + inZ(i), NR, resObs, resExp, RES_BETA);
    const pitRes = ratios((i) => P[i] * 2 + inZ(i), NR, resObs, resExp, RES_BETA);
    const bipObs = (i) => (ZN[i] == null ? -1 : bipCls[i]);
    const bipExp = (i) => leagueBip(ZN[i], T[i]);
    const batBip = ratios((i) => B[i], NB, bipObs, bipExp, BIP_BETA);
    const pitBip = ratios((i) => P[i], NB, bipObs, bipExp, BIP_BETA);

    function resultProbs(q, type, zone) {
      let p = leagueRes(zone, q.balls, q.strikes, type);
      const z = zone < 9 ? 1 : 0, rb = q.batter >= 0 ? batRes.get(q.batter * 2 + z) : null, rp = pitRes.get(q.pitcher * 2 + z);
      if (rb) p = p.map((v, k) => v * rb[k]);
      if (rp) p = p.map((v, k) => v * rp[k]);
      return norm(p);
    }
    function bipProbs(q, type, zone) {
      let p = leagueBip(zone, type);
      const rb = q.batter >= 0 ? batBip.get(q.batter) : null, rp = pitBip.get(q.pitcher);
      if (rb) p = p.map((v, k) => v * rb[k]);
      if (rp) p = p.map((v, k) => v * rp[k]);
      return norm(p);
    }

    // 투수 제구 보정: 코스 모델은 구종·타석별로 잘게 나눠 수축하므로 투수 전체의 존 안 비율이 평균 쪽으로 쏠린다.
    // 이 투수의 실제 존 안 비율 / 코스 모델이 기대한 존 안 비율을 구해 존 안·밖 확률에 곱한다.
    const zoneRatioCache = new Map();
    function zoneRatio(pitcher) {
      let r = zoneRatioCache.get(pitcher);
      if (r) return r;
      const memo = new Map();
      let obsIn = 0, expIn = 0, n = 0;
      for (let i = 0; i < N; i++) {
        if (P[i] !== pitcher || ZN[i] == null) continue;
        const b = balls(S[i]), s = strikes(S[i]), st = lefty(S[i]), key = `${T[i]}|${st}|${b}${s}`;
        let pin = memo.get(key);
        if (pin == null) {
          const zp = PM.zoneProbs({ pitcher, stance: st, balls: b, strikes: s }, T[i]).probs.map((v, z) => v * zoneAdj[b * 3 + s][z]);
          const sum = zp.reduce((a, v) => a + v, 0);
          memo.set(key, (pin = zp.slice(0, 9).reduce((a, v) => a + v, 0) / sum));
        }
        obsIn += ZN[i] < 9; expIn += pin; n++;
      }
      const e = n ? expIn / n : 0.5, o = n ? (obsIn + ZONE_BETA * e) / (n + ZONE_BETA) : e;
      r = [(1 - o) / (1 - e), o / e];  // [존 밖 배율, 존 안 배율]
      zoneRatioCache.set(pitcher, r);
      return r;
    }

    // 투수별 실제 공 (구종|코스|타석) -> 인덱스 목록. 시뮬레이션한 공의 구속·위치·궤적으로 쓴다
    const realCache = new Map();
    function realPitches(pitcher) {
      let m = realCache.get(pitcher);
      if (m) return m;
      m = new Map();
      for (let i = 0; i < N; i++) {
        if (P[i] !== pitcher || ZN[i] == null) continue;
        for (const key of [`${T[i]}|${ZN[i]}|${lefty(S[i])}`, `${T[i]}|${ZN[i]}`, `${T[i]}`]) {
          let a = m.get(key); if (!a) m.set(key, (a = [])); a.push(i);
        }
      }
      realCache.set(pitcher, m);
      return m;
    }

    /* 투수가 다음에 던질 공: 구종 -> 코스 -> 같은 구종·코스의 실제 공 하나 */
    function nextPitch(m, b, s, prev, rng) {
      const real = realPitches(m.pitcher);
      const typeMemo = m._typeMemo || (m._typeMemo = new Map());
      const q = { pitcher: m.pitcher, batter: m.batter, stance: m.stance, balls: b, strikes: s, prev };
      const key = `${b}${s}|${prev}`;
      let tp = typeMemo.get(key);
      if (!tp) typeMemo.set(key, (tp = PM.typeProbs(q).probs));
      const st = m.strategy || {};
      const type = b === 0 && s === 0 && st.first >= 0 ? st.first : s === 2 && st.two >= 0 ? st.two : pick(tp, rng());
      const zkey = `z${type}|${b}${s}`;
      let zp = typeMemo.get(zkey);
      if (!zp) {
        const zr = zoneRatio(m.pitcher);
        typeMemo.set(zkey, (zp = norm(PM.zoneProbs(q, type).probs.map((v, z) => v * zoneAdj[b * 3 + s][z] * zr[z < 9 ? 1 : 0]))));
      }
      const zone = pick(zp, rng());
      const pool = real.get(`${type}|${zone}|${m.stance}`) || real.get(`${type}|${zone}`) || real.get(`${type}`);
      return { type, zone, real: pool ? pool[Math.floor(rng() * pool.length)] : -1, q };
    }

    /* m: { pitcher, batter(-1 = 리그 평균 타자), stance, strategy: { first: 구종|-1, two: 구종|-1 } } */
    function simulatePA(m, rng) {
      let b = 0, s = 0, prev = -1;
      const pitches = [];
      for (let k = 0; k < 25; k++) {
        const { type, zone, real: realIdx, q } = nextPitch(m, b, s, prev, rng);
        const res = pick(resultProbs(q, type, zone), rng());
        const pitch = { type, zone, res, balls: b, strikes: s, real: realIdx };
        pitches.push(pitch);
        prev = type;
        if (res === 0) { b++; if (b === 4) return { pitches, result: "볼넷" }; }
        else if (res === 1 || res === 2) { s++; if (s === 3) return { pitches, result: "삼진" }; }
        else if (res === 3) { if (s < 2) s++; }
        else if (res === 5) return { pitches, result: "사구" };
        else { pitch.bip = pick(bipProbs(q, type, zone), rng()); return { pitches, result: BIP[pitch.bip] }; }
      }
      return { pitches, result: "땅볼" };  // 파울이 끝없이 이어지는 드문 경우
    }

    /* 여러 타석을 돌려 결과 분포를 낸다 */
    function batch(m, n, seed) {
      const rng = mulberry32(seed);
      const counts = {}, finish = new Array(nTypes).fill(0);
      let pitchesN = 0;
      const mm = { ...m, _typeMemo: new Map() };
      for (let k = 0; k < n; k++) {
        const r = simulatePA(mm, rng);
        counts[r.result] = (counts[r.result] || 0) + 1;
        pitchesN += r.pitches.length;
        if (r.result === "삼진") finish[r.pitches[r.pitches.length - 1].type]++;
      }
      return { n, counts, pitchesPerPA: pitchesN / n, strikeoutPitch: finish };
    }

    /* ================= 직접 해보기(게임) ================= */
    const X = ctx.X, Z = ctx.Z;  // 홈플레이트 통과 위치 (1/100 ft, 높이는 리그 평균 존에 맞춰 정규화)

    // 공 하나의 결과로 카운트를 진행한다. 타석이 끝나면 end에 결과 이름
    function advance(b, s, res, bip) {
      if (res === 0) { b++; if (b === 4) return { balls: b, strikes: s, end: "볼넷" }; }
      else if (res === 1 || res === 2) { s++; if (s === 3) return { balls: b, strikes: s, end: "삼진" }; }
      else if (res === 3) { if (s < 2) s++; }
      else if (res === 5) return { balls: b, strikes: s, end: "사구" };
      else if (res === 4) return { balls: b, strikes: s, end: BIP[bip] };
      return { balls: b, strikes: s, end: null };
    }

    // 심판: 타자가 치지 않은 공이 스트라이크로 불린 비율 (코스, 카운트 상황). 2026 실제 판정 기준
    const callT = new Map(), callN = new Map();
    for (let i = 0; i < N; i++) {
      if ((cls[i] !== 0 && cls[i] !== 1) || ZN[i] == null) continue;
      const z = ZN[i], k = z * 4 + cg(balls(S[i]), strikes(S[i]));
      for (const key of [k, 100 + z]) { callN.set(key, (callN.get(key) || 0) + 1); callT.set(key, (callT.get(key) || 0) + (cls[i] === 1)); }
    }
    function calledStrikeProb(zone, b, s) {
      const zp = (callT.get(100 + zone) || 0) / (callN.get(100 + zone) || 1), k = zone * 4 + cg(b, s);
      return ((callT.get(k) || 0) + 20 * zp) / ((callN.get(k) || 0) + 20);
    }

    // 투수 모드: 노린 곳 근처에서 이 투수가 이 구종으로 실제 던진 공 하나를 고른다.
    // 흩어짐은 이 투수의 그 구종 실제 위치 분산에 비례해서, 제구가 좋은 투수일수록 노린 곳에 잘 간다.
    const AIM_K = 0.6;
    const spreadCache = new Map();
    function pitchesOf(pitcher, type) {
      const key = pitcher * 64 + type;
      let e = spreadCache.get(key);
      if (e) return e;
      const idx = [];
      let mx = 0, mz = 0;
      for (let i = 0; i < N; i++) if (P[i] === pitcher && T[i] === type && X[i] != null && Z[i] != null) { idx.push(i); mx += X[i]; mz += Z[i]; }
      mx /= idx.length || 1; mz /= idx.length || 1;
      let vx = 0, vz = 0;
      for (const i of idx) { vx += (X[i] - mx) ** 2; vz += (Z[i] - mz) ** 2; }
      e = { idx, sx: Math.sqrt(vx / (idx.length || 1)) / 100, sz: Math.sqrt(vz / (idx.length || 1)) / 100 };
      spreadCache.set(key, e);
      return e;
    }
    function aimPitch(pitcher, type, x, z, rng, k = AIM_K) {
      const { idx, sx, sz } = pitchesOf(pitcher, type);
      if (!idx.length) return null;
      const ax = Math.max(0.15, k * sx), az = Math.max(0.15, k * sz);
      const w = idx.map((i) => Math.exp(-(((X[i] / 100 - x) / ax) ** 2 + ((Z[i] / 100 - z) / az) ** 2) / 2));
      const sum = w.reduce((a, v) => a + v, 0);
      const i = sum > 0 ? idx[pick(w.map((v) => v / sum), rng())] : idx[Math.floor(rng() * idx.length)];
      return { real: i, zone: ZN[i], x: X[i] / 100, z: Z[i] / 100, spread: [sx, sz] };
    }
    // 투수 모드: 던진 공에 대한 타자 반응 (시뮬레이터와 같은 결과 모델)
    function batterReacts(m, b, s, type, zone, rng) {
      const q = { pitcher: m.pitcher, batter: m.batter, stance: m.stance, balls: b, strikes: s };
      const res = pick(resultProbs(q, type, zone), rng());
      return { res, bip: res === 4 ? pick(bipProbs(q, type, zone), rng()) : null };
    }
    // 투수 모드 힌트: 코스별로 이 공을 던지면 생기는 기대 실점 변화 (타자 기준, 음수가 투수에게 좋음)
    function zoneValues(m, b, s, type, countValue, paValue) {
      const q = { pitcher: m.pitcher, batter: m.batter, stance: m.stance, balls: b, strikes: s };
      const cv = (bb, ss) => countValue[`${Math.min(bb, 3)}${Math.min(ss, 2)}`] || 0, now = cv(b, s);
      const bipName = { "직선타": "라인드라이브", "실책": "실책출루" };
      const out = [];
      for (let zone = 0; zone < NZ; zone++) {
        const pr = resultProbs(q, type, zone), bp = bipProbs(q, type, zone);
        let v = 0;
        pr.forEach((p, res) => {
          if (res === 4) { bp.forEach((pb, k) => { v += p * pb * ((paValue[bipName[BIP[k]] || BIP[k]] || 0) - now); }); return; }
          const a = advance(b, s, res, null);
          v += p * ((a.end ? paValue[a.end] || 0 : cv(a.balls, a.strikes)) - now);
        });
        out.push({ zone, value: v, whiff: pr[2] / (pr[2] + pr[3] + pr[4]) });
      }
      return out;
    }

    // 타자 모드: 사용자의 스윙 판정. dt는 공 시간 기준 타이밍 오차(초, +가 늦음), dx·dz는 친 위치와 공의 좌우·높이 차이(ft).
    // 배트는 가로로 길어서 좌우 오차(0.8ft ≈ 24cm)는 높이 오차(0.4ft ≈ 12cm)보다 관대하게 본다.
    const SWING = { dt: 0.075, dx: 0.8, dz: 0.4 };
    const TILT = [0.6, 1.4, 1.4, 2.4, -0.6, -0.2, 0.2, 0];  // 잘 맞을수록 1루타~홈런 쪽으로, 빗맞으면 땅볼 쪽으로
    function swingResult(m, b, s, type, zone, swing, rng) {
      const q = { pitcher: m.pitcher, batter: m.batter, stance: m.stance, balls: b, strikes: s };
      if (!swing) return { res: rng() < calledStrikeProb(zone, b, s) ? 1 : 0, bip: null, quality: null };
      const e = Math.hypot(swing.dt / SWING.dt, swing.dx / SWING.dx, swing.dz / SWING.dz);
      if (e > 1) return { res: 2, bip: null, quality: 0, miss: e };
      const quality = 1 - e;
      if (rng() < 0.12 + 0.55 * e) return { res: 3, bip: null, quality };  // 빗맞으면 파울이 많다
      const base = bipProbs(q, type, zone);
      const p = norm(base.map((v, k) => v * Math.exp(TILT[k] * (quality - 0.5) * 2)));
      return { res: 4, bip: pick(p, rng()), quality };
    }

    return { simulatePA, batch, resultProbs, bipProbs, rng: mulberry32,
      advance, calledStrikeProb, aimPitch, batterReacts, zoneValues, swingResult, pitchesOf, nextPitch, SWING };
  }

  const api = { create, RESULTS, BIP, mulberry32 };
  root.BallSim = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
