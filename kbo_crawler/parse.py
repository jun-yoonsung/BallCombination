"""네이버 relay / schedule 응답 JSON을 투구 단위 행(dict)으로 변환한다.

필드 이름은 네이버가 바꿀 수 있으므로 없는 값은 None으로 두고 죽지 않게 한다.
"""

PITCH_COLUMNS = [
    "game_id", "inning", "half", "seqno", "pitch_num",
    "pitcher_id", "pitcher_name", "batter_id", "batter_name", "stance",
    "balls", "strikes", "outs", "pitch_result", "pitch_type", "speed_kmh",
    "plate_x", "plate_z", "sz_top", "sz_bot",
    "x0", "y0", "z0", "vx0", "vy0", "vz0", "ax", "ay", "az",
    "pts_pitch_id", "text",
]

# ptsOptions 필드 -> 출력 컬럼
PTS_FIELDS = {
    "crossPlateX": "plate_x",
    "crossPlateY": "plate_z",
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


def parse_relay(data, game_id, inning, names=None):
    """relay 응답 하나(한 이닝)를 투구 행 리스트로 변환한다."""
    names = names if names is not None else player_names(data)
    relays = (_result(data).get("textRelayData") or {}).get("textRelays") or []
    rows = []
    for relay in relays:
        pts = {str(p.get("pitchId")): p for p in relay.get("ptsOptions") or []}
        # homeOrAway "0" = 원정팀 공격(초), "1" = 홈팀 공격(말)
        hoa = str(relay.get("homeOrAway", ""))
        half = {"0": "top", "1": "bottom"}.get(hoa)
        for opt in relay.get("textOptions") or []:
            if not is_pitch(opt):
                continue
            state = opt.get("currentGameState") or {}
            pitcher_id = state.get("pitcher")
            batter_id = state.get("batter")
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
                "balls": state.get("ball"),
                "strikes": state.get("strike"),
                "outs": state.get("out"),
                "pitch_result": opt.get("pitchResult"),
                "pitch_type": opt.get("stuff"),
                "speed_kmh": _num(opt.get("speed")),
                "pts_pitch_id": opt.get("ptsPitchId"),
                "text": opt.get("text"),
            }
            for src, dst in PTS_FIELDS.items():
                row[dst] = _num(p.get(src)) if src != "stance" else p.get(src)
            rows.append(row)
    rows.sort(key=lambda r: (r["seqno"] is None, r["seqno"] or 0))
    return rows


def parse_games(schedule_data, finished_only=True):
    """schedule 응답에서 경기 목록을 뽑는다."""
    games = []
    for g in _result(schedule_data).get("games") or []:
        if g.get("categoryId") not in (None, "kbo"):
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
            "status": status,
        })
    return games
