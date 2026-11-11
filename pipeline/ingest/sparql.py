"""Wikidata Query Service: templated queries, run one at a time."""

from pathlib import Path
from string import Template

from .http import RateLimiter, WikimediaClient

QUERY_DIR = Path(__file__).parent / "queries"
ENTITY_PREFIX = "http://www.wikidata.org/entity/"


def qid(uri: str) -> str:
    return uri.removeprefix(ENTITY_PREFIX)


class Sparql:
    def __init__(self, client: WikimediaClient, endpoint: str, pause_seconds: float):
        self.client = client
        self.endpoint = endpoint
        self.limiter = RateLimiter(pause_seconds)

    def select(self, template: str, dump_name: str, **params) -> list[dict[str, str]]:
        """Runs queries/<template>.rq and returns bindings as {var: value}."""
        text = Template((QUERY_DIR / f"{template}.rq").read_text()).substitute(**params)
        data = self.client.post_form(
            self.endpoint, {"query": text}, limiter=self.limiter, dump_name=f"sparql_{dump_name}",
            accept="application/sparql-results+json",
        )
        return [{k: v["value"] for k, v in row.items()} for row in data["results"]["bindings"]]
