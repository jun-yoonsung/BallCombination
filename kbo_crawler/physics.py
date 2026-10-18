"""PITCHf/x 9-파라미터 궤적으로 투구 위치와 무브먼트를 계산한다.

좌표계(단위 ft, s): 원점은 홈플레이트 뒤 꼭짓점, x는 포수 기준 오른쪽(1루 쪽), y는 투수 쪽, z는 위.
위치 r(t) = r0 + v0*t + a*t^2/2  (가속도 a는 비행 중 일정하다고 가정)

- 무브먼트(pfx): y=40ft부터 홈플레이트까지, 중력만 받는 직선 궤적 대비 공이 얼마나 더 움직였는지.
  PITCHf/x와 같은 정의라서 수직 무브먼트는 중력을 뺀 값이다(직구는 +, 커브는 -).

회전수(rpm)는 계산하지 않는다. 마그누스 가속도로 회전수를 추정해 보면 같은 투수의 직구도 공마다
2,200~3,900rpm으로 흔들려서(가속도 잡음이 추정식에서 크게 증폭됨) 쓸 수 있는 값이 아니었다.
"""

import math

G = 32.174  # ft/s^2
Y_PLATE = 17 / 12  # 홈플레이트 앞면
Y_PFX = 40.0  # PITCHf/x 무브먼트 측정 시작
FT = 0.3048

PARAMS = ("x0", "y0", "z0", "vx0", "vy0", "vz0", "ax", "ay", "az")


def time_to_y(p, y):
    """y0에서 출발한 공이 y에 도달하는 시간(초). 도달하지 못하면 None."""
    a, b, c = p["ay"] / 2, p["vy0"], p["y0"] - y
    if a == 0:
        return -c / b if b else None
    disc = b * b - 4 * a * c
    if disc < 0:
        return None
    return (-b - math.sqrt(disc)) / (2 * a)


def position(p, t):
    return (
        p["x0"] + p["vx0"] * t + p["ax"] * t * t / 2,
        p["y0"] + p["vy0"] * t + p["ay"] * t * t / 2,
        p["z0"] + p["vz0"] * t + p["az"] * t * t / 2,
    )


def movement(p):
    """(수평, 수직) 무브먼트, 단위 cm. 수평은 포수 시점 +가 1루 쪽."""
    t40, tp = time_to_y(p, Y_PFX), time_to_y(p, Y_PLATE)
    if t40 is None or tp is None:
        return None
    t = tp - t40
    return (p["ax"] * t * t / 2 * FT * 100, (p["az"] + G) * t * t / 2 * FT * 100)


def from_row(row):
    """CSV 행(dict of str)에서 궤적 파라미터를 꺼낸다. 하나라도 없으면 None."""
    try:
        return {k: float(row[k]) for k in PARAMS}
    except (KeyError, TypeError, ValueError):
        return None
