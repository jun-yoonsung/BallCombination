"""네이버 relay / schedule 응답 JSON을 투구 단위 행(dict)으로 변환한다.

필드 이름은 네이버가 바꿀 수 있으므로 없는 값은 None으로 두고 죽지 않게 한다.

좌표계는 PITCHf/x 관례(단위 ft, 포수 시점)를 따른다. ptsOptions의 crossPlateY는
높이가 아니라 홈플레이트 앞면까지의 거리(y, 항상 약 0.708ft)이므로, 홈플레이트 통과
높이(plate_z)는 궤적 파라미터로 직접 계산한다.
"""

import math

PITCH_COLUMNS = [
    "game_id", "inning", "half", "seqno", "pitch_num",
    "pitcher_id", "pitcher_name", "batter_id", "batter_name", "stance",
    "balls", "strikes", "outs", "on_1b", "on_2b", "on_3b",
    "away_score", "home_score", "score_diff",
    "pitch_result", "pitch_type", "speed_kmh",
    "pa_result", "pa_last_pitch", "pa_result_text",
    "plate_x", "plate_z", "sz_top", "sz_bot",
    "x0", "y0", "z0", "vx0", "vy0", "vz0", "ax", "ay", "az",
    "pts_pitch_id", "text",
]

# ptsOptions 필드 -> 출력 컬럼
PTS_FIELDS = {
    "crossPlateX": "plate_x",
    "topSz": "sz_top",
    "bottomSz": "sz_bot",
    "x0": "x0", "y0": "y0", "z0": "z0",
    "vx0": "vx0", "vy0": "vy0", "vz0": "vz0",
    "ax": "ax", "ay": "ay", "az": "az",
    "stance": "stance",
}


def _num(v):
    if v in (None, ""):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return int(f) if f.is_integer() and "." not in str(v) else f


def plate_z(p, y_plate=17 / 24):
    """궤적 y(t) = y0 + vy0*t + ay*t^2/2 가 y_plate에 닿는 시점의 높이 z(t)."""
    try:
        y0, vy0, ay = float(p["y0"]), float(p["vy0"]), float(p["ay"])
        z0, vz0, az = float(p["z0"]), float(p["vz0"]), float(p["az"])
        y_plate = float(p.get("crossPlateY", y_plate))
    except (KeyError, TypeError, ValueError):
        return None
    a, b, c = ay / 2, vy0, y0 - y_plate
    disc = b * b - 4 * a * c
    if a == 0 or disc < 0:
        return None
    t = (-b - math.sqrt(disc)) / (2 * a)
    return round(z0 + vz0 * t + az * t * t / 2, 4)


# 타석 결과 문구 -> 범주. 위에서부터 먼저 맞는 규칙을 쓴다.
PA_RULES = [
    (("낫 아웃", "낫아웃", "삼진"), "삼진"),
    (("실책",), "실책출루"),
    (("고의4구",), "고의4구"),
    (("볼넷",), "볼넷"),
    (("몸에 맞는",), "사구"),
    (("홈런",), "홈런"),
    (("3루타",), "3루타"),
    (("2루타",), "2루타"),
    (("1루타", "안타"), "1루타"),
    (("희생플라이",), "희생플라이"),
    (("희생번트",), "희생번트"),
    (("병살", "삼중살"), "병살타"),
    (("야수선택",), "야수선택"),
    (("땅볼",), "땅볼"),
    (("라인드라이브",), "라인드라이브"),
    (("플라이",), "플라이"),
    (("번트",), "번트아웃"),
]
PA_RESULT_EVENTS = (13, 23)  # 13 타자 결과, 23 장타/홈런 (14, 24는 주자 이벤트)
PA_START_EVENTS = (0, 8)  # 0 공수 교대, 8 타자 등장


def classify_pa(text):
    """'황영묵 : 우익수 오른쪽 뒤 3루타' -> '3루타'."""
    body = text.split(" : ", 1)[-1]
    for keys, label in PA_RULES:
        if any(k in body for k in keys):
            return label
    return "기타"


def _result(data):
    return (data or {}).get("result") or {}


def player_names(data):
    """응답 어디에든 있는 {pcode, name} 쌍을 모아 선수코드 -> 이름 맵을 만든다."""
    names = {}

    def walk(o):
        if isinstance(o, dict):
            code, name = o.get("pcode") or o.get("playerCode"), o.get("name") or o.get("playerName")
            if code and isinstance(name, str):
                names[str(code)] = name
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(_result(data))
    return names


def is_pitch(opt):
    return bool(opt.get("ptsPitchId") or opt.get("stuff") or (opt.get("pitchNum") or 0) > 0)


def _on_base(v):
    # base1~3 값은 0이면 빈 루, 아니면 그 주자의 타순 번호
    n = _num(v)
    return None if n is None else int(bool(n))


def parse_relay(data, game_id, inning, names=None):
    """relay 응답 하나(한 이닝)를 투구 행 리스트로 변환한다.

    currentGameState는 해당 이벤트 '이후' 상태이므로, 투구 직전 상황(카운트, 아웃, 주자, 점수)은
    seqno 순으로 바로 앞 이벤트의 상태를 쓴다.
    """
    names = names if names is not None else player_names(data)
    relays = (_result(data).get("textRelayData") or {}).get("textRelays") or []
    events = []
    for relay in relays:
        pts = {str(p.get("pitchId")): p for p in relay.get("ptsOptions") or []}
        for opt in relay.get("textOptions") or []:
            events.append((opt.get("seqno") or 0, relay, opt, pts))
    events.sort(key=lambda e: e[0])

    rows = []
    pa_rows = []  # 결과가 아직 안 나온 현재 타석의 투구들
    prev = None
    for _, relay, opt, pts in events:
        state = opt.get("currentGameState") or {}
        before = prev if prev is not None else state
        prev = state
        etype = opt.get("type")
        if etype in PA_START_EVENTS:
            pa_rows = []
        elif etype in PA_RESULT_EVENTS and pa_rows:
            text = opt.get("text") or ""
            for r in pa_rows:
                r["pa_result"], r["pa_result_text"] = classify_pa(text), text
            pa_rows[-1]["pa_last_pitch"] = 1
            pa_rows = []
        if not is_pitch(opt):
            continue
        # homeOrAway "0" = 원정팀 공격(초), "1" = 홈팀 공격(말)
        half = {"0": "top", "1": "bottom"}.get(str(relay.get("homeOrAway", "")))
        pitcher_id = state.get("pitcher")
        batter_id = state.get("batter")
        away, home = _num(before.get("awayScore")), _num(before.get("homeScore"))
        score_diff = None
        if half and away is not None and home is not None:
            score_diff = home - away if half == "top" else away - home
        p = pts.get(str(opt.get("ptsPitchId")), {})
        row = {
            "game_id": game_id,
            "inning": relay.get("inn", inning),
            "half": half,
            "seqno": opt.get("seqno"),
            "pitch_num": opt.get("pitchNum"),
            "pitcher_id": pitcher_id,
            "pitcher_name": names.get(str(pitcher_id)),
            "batter_id": batter_id,
            "batter_name": names.get(str(batter_id)),
            "balls": _num(before.get("ball")),
            "strikes": _num(before.get("strike")),
            "outs": _num(before.get("out")),
            "on_1b": _on_base(before.get("base1")),
            "on_2b": _on_base(before.get("base2")),
            "on_3b": _on_base(before.get("base3")),
            "away_score": away,
            "home_score": home,
            "score_diff": score_diff,
            "pitch_result": opt.get("pitchResult"),
            "pitch_type": opt.get("stuff"),
            "speed_kmh": _num(opt.get("speed")),
            "pa_result": None,
            "pa_last_pitch": 0,
            "pa_result_text": None,
            "pts_pitch_id": opt.get("ptsPitchId"),
            "text": opt.get("text"),
        }
        for src, dst in PTS_FIELDS.items():
            row[dst] = _num(p.get(src)) if src != "stance" else p.get(src)
        row["plate_z"] = plate_z(p)
        rows.append(row)
        pa_rows.append(row)
    return rows


def is_official_round(round_code):
    """정규시즌(kbo_r) 또는 포스트시즌(kbo_ps_wd 와일드카드, kbo_ps_sp 준PO 등)."""
    return round_code == "kbo_r" or str(round_code).startswith("kbo_ps")


def parse_games(schedule_data, finished_only=True, official_only=True):
    """schedule 응답에서 경기 목록을 뽑는다. 기본은 정규시즌 + 포스트시즌만 (시범경기, 올스타전 제외)."""
    games = []
    for g in _result(schedule_data).get("games") or []:
        if g.get("categoryId") not in (None, "kbo"):
            continue
        if official_only and not is_official_round(g.get("roundCode")):
            continue
        status = g.get("statusCode")
        if finished_only and (g.get("cancel") or status != "RESULT"):
            continue
        games.append({
            "game_id": g.get("gameId"),
            "game_date": g.get("gameDate"),
            "away_team": g.get("awayTeamName"),
            "home_team": g.get("homeTeamName"),
            "away_score": g.get("awayTeamScore"),
            "home_score": g.get("homeTeamScore"),
            "stadium": g.get("stadium"),
            # kbo_r 정규시즌, kbo_ps_* 포스트시즌, kbo_e 시범경기, kbo_as 올스타전
            "round_code": g.get("roundCode"),
            "status": status,
        })
    return games
