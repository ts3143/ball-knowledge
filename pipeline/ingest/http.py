"""Shared HTTP client for Wikimedia APIs: User-Agent, pacing, retries, raw dumps."""

import gzip
import json
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import httpx

RETRY_STATUSES = {429, 500, 502, 503, 504}


class RateLimiter:
    """Spaces calls at least `interval` seconds apart, across threads."""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            time.sleep(delay)


class WikimediaClient:
    def __init__(self, contact: str, timeout: float, max_retries: int, raw_dir: Path | None = None):
        self.client = httpx.Client(
            headers={"User-Agent": f"BallKnowledge-ingest/0.1 ({contact})", "Accept-Encoding": "gzip"},
            timeout=timeout,
            follow_redirects=True,
        )
        self.max_retries = max_retries
        self.raw_dir = raw_dir
        self._dump_lock = threading.Lock()
        self._dump_count = 0
        self.requests: Counter = Counter()  # HTTP requests sent, by host (retries included)

    def get_json(self, url: str, params: dict | None = None, limiter: RateLimiter | None = None,
                 allow_404: bool = False, dump_name: str | None = None) -> dict | None:
        for attempt in range(self.max_retries + 1):
            if limiter:
                limiter.wait()
            self.requests[urlparse(url).netloc] += 1
            try:
                resp = self.client.get(url, params=params)
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise
                self._backoff(attempt, None, f"{type(exc).__name__}")
                continue
            if resp.status_code == 404 and allow_404:
                return None
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                self._backoff(attempt, resp.headers.get("Retry-After"), f"HTTP {resp.status_code}")
                continue
            resp.raise_for_status()
            data = resp.json()
            # MediaWiki API reports replica lag as an error body with HTTP 200.
            if isinstance(data, dict) and data.get("error", {}).get("code") == "maxlag" and attempt < self.max_retries:
                self._backoff(attempt, resp.headers.get("Retry-After"), "maxlag")
                continue
            if dump_name:
                self.dump(dump_name, data)
            return data
        raise RuntimeError(f"Gave up on {url}")

    def post_form(self, url: str, data: dict, limiter: RateLimiter | None = None,
                  dump_name: str | None = None, accept: str = "application/json") -> dict:
        for attempt in range(self.max_retries + 1):
            if limiter:
                limiter.wait()
            self.requests[urlparse(url).netloc] += 1
            try:
                resp = self.client.post(url, data=data, headers={"Accept": accept})
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise
                self._backoff(attempt, None, f"{type(exc).__name__}")
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                self._backoff(attempt, resp.headers.get("Retry-After"), f"HTTP {resp.status_code}")
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code} from {url}: {resp.text[:500]}")
            out = resp.json()
            if dump_name:
                self.dump(dump_name, out)
            return out
        raise RuntimeError(f"Gave up on {url}")

    def dump(self, name: str, data: dict) -> None:
        if not self.raw_dir:
            return
        with self._dump_lock:
            self._dump_count += 1
            path = self.raw_dir / f"{self._dump_count:05d}_{name}.json.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None, reason: str) -> None:
        try:
            delay = float(retry_after) if retry_after else 2.0 ** attempt
        except ValueError:
            delay = 2.0 ** attempt
        delay = min(max(delay, 1.0), 120.0)
        print(f"  retrying in {delay:.0f}s ({reason})", file=sys.stderr)
        time.sleep(delay)
