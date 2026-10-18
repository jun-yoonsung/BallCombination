# BallCombination

네이버 스포츠 KBO 문자중계에서 **투구 단위 데이터(구종, 구속, 투구 위치, 궤적)** 를 수집하는 크롤러.

## 설치

```bash
pip install -r requirements.txt
```

## 사용법

```bash
# 기간 지정: 해당 기간에 종료된 KBO 정규시즌/포스트시즌 경기를 모두 수집
python -m kbo_crawler.crawl --from 2025-09-01 --to 2025-09-07 --out data/pitches_0901_0907.csv

# 경기 지정 (gameId는 네이버 경기 URL에 있는 값, 예: 20250920LGSS02025)
python -m kbo_crawler.crawl --game 20250920LGSS02025
```

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--from` / `--to` | | 수집 기간 (YYYY-MM-DD) |
| `--game` | | gameId 직접 지정, 여러 번 사용 가능 |
| `--out` | `data/pitches.csv` | 출력 CSV (Excel에서 한글이 깨지지 않게 UTF-8 BOM으로 저장) |
| `--cache` | `data/raw` | API 원본 JSON 캐시. 다시 실행하면 캐시를 재사용 |
| `--all-rounds` | 꺼짐 | 시범경기·올스타전도 수집 (기본은 정규시즌 + 포스트시즌만) |
| `--delay` | `0.7` | 요청 간 최소 간격(초) |

## 출력 컬럼

| 컬럼 | 설명 |
|---|---|
| `game_id`, `game_date`, `stadium`, `away_team`, `home_team` | 경기 정보 |
| `round_code` | `kbo_r` 정규시즌, `kbo_ps_*` 포스트시즌(`wd` 와일드카드, `sp` 준플레이오프 등) |
| `inning`, `half` | 이닝, `top`(초) / `bottom`(말) |
| `pitcher_id/name`, `batter_id/name`, `stance` | 투수, 타자, 타자 타석(L/R) |
| `balls`, `strikes`, `outs` | 투구 **직전** 볼카운트, 아웃 수 |
| `on_1b`, `on_2b`, `on_3b` | 투구 직전 1·2·3루 주자 유무 (1/0) |
| `away_score`, `home_score` | 투구 직전 점수 |
| `score_diff` | 투구 직전 점수차, **투수팀 기준** (+면 투수팀이 앞섬) |
| `pitch_num`, `pitch_result` | 타석 내 투구 번호, 결과 코드(B 볼, T 루킹 스트라이크, S 헛스윙, F 파울, H 타격 등) |
| `pitch_type`, `speed_kmh` | 구종, 구속(km/h) |
| `pa_result` | 그 투구가 속한 **타석의 최종 결과**: 삼진, 볼넷, 고의4구, 사구, 1루타, 2루타, 3루타, 홈런, 땅볼, 플라이, 라인드라이브, 병살타, 희생번트, 희생플라이, 실책출루, 야수선택, 번트아웃, 기타 |
| `pa_last_pitch` | 타석의 마지막 공(결정구)이면 1 |
| `pa_result_text` | 타석 결과 원문 (예: `황영묵 : 우익수 오른쪽 뒤 3루타`) |
| `plate_x`, `plate_z` | 홈플레이트 통과 위치(좌우, 지면으로부터 높이). 단위 ft, PITCHf/x 좌표계 |
| `sz_top`, `sz_bot` | 해당 타자 스트라이크존 상단/하단 |
| `x0..az` | 궤적 파라미터(초기 위치·속도·가속도) |
| `text` | 문자중계 원문 |

## 동작 방식

1. `GET https://api-gw.sports.naver.com/schedule/games?...&categoryId=kbo` 로 기간 내 경기 목록 조회
2. 경기마다 `GET /schedule/games/{gameId}/relay?inning=N` 을 1회부터 호출 (연장까지, 데이터가 없으면 중단)
3. 응답의 `textOptions`(구종/구속/카운트)와 `ptsOptions`(투구 추적 좌표)를 `ptsPitchId`로 매칭해서 한 행으로 만듦
4. `currentGameState`는 이벤트 **이후** 상태이므로, 투구 직전 상황(카운트, 아웃, 주자, 점수)은
   seqno 순으로 바로 앞 이벤트(타자 등장, 도루, 피치클락 위반 등 포함)의 상태를 사용
5. `ptsOptions.crossPlateY`는 높이가 아니라 홈플레이트 앞면까지의 거리(항상 약 0.708ft)이므로,
   투구 높이 `plate_z`는 궤적 파라미터(`y0, vy0, ay, z0, vz0, az`)로 홈플레이트 통과 시점을 구해 계산함
   (같은 방식으로 계산한 x가 네이버의 `crossPlateX`와 일치하는 것을 확인)

## 주의

- 네이버의 **비공식 내부 API**입니다. 응답 구조가 바뀌면 `kbo_crawler/parse.py`를 수정해야 합니다.
  원본 JSON이 `data/raw/{gameId}/` 에 남아 있으니 브라우저 개발자도구(Network 탭에서 `relay` 검색)와 비교해서 고치면 됩니다.
- 요청 간격을 너무 줄이지 마세요. 시즌 전체(약 720경기)는 1만 회 가까운 요청이 나갑니다.
- 수집한 데이터는 개인 분석이나 연구 용도로 쓰고, 재배포나 상업적 이용은 네이버 이용약관을 확인하세요.

## 테스트

```bash
python -m unittest -v
```

## 웹 탐색기

`web/index.html`은 수집한 투구로 투수별 볼배합을 보는 페이지입니다.

- **투구 위치**: 스트라이크존 위에 구종별 색으로 표시 (포수 시점, 타자별 존을 리그 평균 높이에 맞춰 정규화)
- **구종 구성**: 구종별 투구 수, 비율, 평균/최고 구속, 스트라이크%, 헛스윙%
- **카운트별 볼배합**: 투구 직전 카운트 12가지마다 구종 비율
- **필터**: 팀, 투수, 타자 타석(좌/우), 카운트 상황, 주자, 아웃, 점수 상황, 월, 정규/포스트시즌

```bash
python -m kbo_crawler.export_web "datasets/kbo_pitches/2026-*.csv" --out web/pitches.json
cd web && python -m http.server 8000   # http://localhost:8000
```
