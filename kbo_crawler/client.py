"""네이버 스포츠 내부 API(api-gw.sports.naver.com) 호출 클라이언트.

비공식 API이므로 예고 없이 바뀔 수 있다. 응답 원본은 캐시 디렉터리에 저장해서
파서가 바뀌어도 다시 요청하지 않고 재가공할 수 있게 한다.
"""

import json
import time
from pathlib import Path

import requests

BASE_URL = "https://api-gw.sports.naver.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://m.sports.naver.com/",
    "Accept": "application/json",
}


class NaverSportsClient:
    def __init__(self, cache_dir="data/raw", delay=0.7, retries=3, timeout=10):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._last_request = 0.0

    def _get(self, path, params=None):
        url = BASE_URL + path
        for attempt in range(1, self.retries + 1):
            wait = self.delay - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code not in (429, 500, 502, 503, 504):
                    resp.raise_for_status()
            except (requests.ConnectionError, requests.Timeout):
                if attempt == self.retries:
                    raise
            time.sleep(2 ** attempt)
        raise RuntimeError(f"요청 실패: {url} {params}")

    def _cached(self, rel_path, fetch):
        if self.cache_dir is None:
            return fetch()
        path = self.cache_dir / rel_path
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        data = fetch()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def schedule(self, from_date, to_date):
        """from_date/to_date: 'YYYY-MM-DD'. 기간 내 KBO 경기 목록(원본 JSON)."""
        params = {
            "fields": "basic",
            "upperCategoryId": "kbaseball",
            "categoryId": "kbo",
            "fromDate": from_date,
            "toDate": to_date,
            "size": 500,
        }
        # 일정은 진행 중 경기 상태가 바뀌므로 캐시하지 않는다.
        return self._get("/schedule/games", params)

    def relay(self, game_id, inning, use_cache=True):
        """한 경기의 특정 이닝 문자중계 + 투구 추적(PTS) 데이터."""
        fetch = lambda: self._get(f"/schedule/games/{game_id}/relay", {"inning": inning})
        if not use_cache:
            return fetch()
        return self._cached(f"{game_id}/inning_{inning:02d}.json", fetch)
