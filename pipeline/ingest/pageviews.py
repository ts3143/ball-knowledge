"""Wikipedia monthly pageviews per article, cached on disk so re-runs fetch only new months."""

import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from urllib.parse import quote

from .http import RateLimiter, WikimediaClient

API = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"


def last_complete_month(today: date) -> date:
    return date(today.year - 1, 12, 1) if today.month == 1 else date(today.year, today.month - 1, 1)


def _month(s: str) -> date:
    return date(int(s[:4]), int(s[5:7]), 1)


def _next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def _month_end(d: date) -> date:
    return date.fromordinal(_next_month(d).toordinal() - 1)


class Pageviews:
    def __init__(self, client: WikimediaClient, cache_dir: Path, cfg: dict, today: date):
        self.client = client
        self.cache_dir = cache_dir
        self.agent = cfg["agent"]
        self.start = _month(cfg["start_month"])
        self.through = last_complete_month(today)
        self.limiter = RateLimiter(1.0 / cfg["max_requests_per_second"])
        self.concurrency = cfg["concurrency"]

    def _cache_path(self, wiki: str, title: str) -> Path:
        digest = hashlib.sha1(title.encode()).hexdigest()
        return self.cache_dir / "pageviews" / wiki / digest[:2] / f"{digest}.json"

    def monthly(self, wiki: str, title: str) -> tuple[dict[str, int], bool]:
        """({'YYYY-MM': views}, fetched_from_network) for one article."""
        path = self._cache_path(wiki, title)
        cached = json.loads(path.read_text()) if path.exists() else None
        if cached and cached["title"] == title and _month(cached["through"]) >= self.through:
            return cached["months"], False
        months = cached["months"] if cached and cached["title"] == title else {}
        begin = _next_month(_month(cached["through"])) if cached and cached["title"] == title else self.start
        url = (f"{API}/{wiki}.wikipedia.org/all-access/{self.agent}/{quote(title.replace(' ', '_'), safe='')}"
               f"/monthly/{begin:%Y%m%d}/{_month_end(self.through):%Y%m%d}")
        data = self.client.get_json(url, limiter=self.limiter, allow_404=True)
        for item in (data or {}).get("items", []):
            ts = item["timestamp"]
            months[f"{ts[:4]}-{ts[4:6]}"] = item["views"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"title": title, "through": f"{self.through:%Y-%m}", "months": months}))
        return months, True

    def fetch_all(self, articles: list[tuple[str, str, str]]) -> list[tuple[str, str, date, int]]:
        """articles: (wikidata_id, wiki, title). Returns (wikidata_id, wiki, month, views) rows."""
        rows: list[tuple[str, str, date, int]] = []
        fetched = 0

        def one(article):
            qid, wiki, title = article
            months, from_network = self.monthly(wiki, title)
            return qid, wiki, months, from_network

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            for i, (qid, wiki, months, from_network) in enumerate(pool.map(one, articles), 1):
                fetched += from_network
                if i % 500 == 0 or i == len(articles):
                    print(f"  pageviews: {i}/{len(articles)} ({fetched} fetched, rest cached)", file=sys.stderr)
                for m, views in months.items():
                    if views > 0:
                        rows.append((qid, wiki, _month(m), views))
        return rows
