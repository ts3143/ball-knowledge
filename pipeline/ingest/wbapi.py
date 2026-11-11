"""Wikidata entity API (wbgetentities): labels, aliases, sitelinks and full statements."""

import sys

from .http import RateLimiter, WikimediaClient


class EntityApi:
    def __init__(self, client: WikimediaClient, endpoint: str, batch: int, pause_seconds: float,
                 languages: list[str]):
        self.client = client
        self.endpoint = endpoint
        self.batch = batch
        self.limiter = RateLimiter(pause_seconds)
        self.languages = languages

    def fetch(self, ids: list[str], label: str, with_claims: bool = True,
              all_languages: bool = False) -> dict[str, dict]:
        """Returns {requested id: entity json}. Missing items are left out; a redirected id
        maps to its target's entity (whose "id" differs from the key)."""
        props = "labels|aliases|descriptions|sitelinks" + ("|claims" if with_claims else "")
        out: dict[str, dict] = {}
        unique = sorted(set(ids), key=lambda q: int(q[1:]))
        for i in range(0, len(unique), self.batch):
            chunk = unique[i:i + self.batch]
            if len(unique) > self.batch:
                print(f"  {label}: {i + len(chunk)}/{len(unique)}", file=sys.stderr)
            params = {
                "action": "wbgetentities",
                "ids": "|".join(chunk),
                "props": props,
                "sitefilter": "dewiki|enwiki",
                "format": "json",
                "maxlag": "5",
            }
            if not all_languages:
                params["languages"] = "|".join(self.languages)
            data = self.client.get_json(self.endpoint, params=params, limiter=self.limiter,
                                        dump_name=f"entities_{label}")
            if "error" in data:
                raise RuntimeError(f"wbgetentities error: {data['error']}")
            redirects = {}
            for key, entity in data.get("entities", {}).items():
                if "missing" in entity:
                    continue
                out[key] = entity
                if "redirects" in entity:
                    redirects[entity["redirects"]["from"]] = entity
            for source, entity in redirects.items():
                out[source] = entity
        return out
