"""German Wikipedia infobox coaching tables: the second source for coach spells.

Exception to "Wikidata + pageviews only", approved by T on 2026-11-11, for coach spells only.
Wikidata often links a coach to a club without dates, or not at all. The `trainer_tabelle`
of dewiki's "Infobox Fußballspieler" lists each coaching station with years and a role note:

    {{Team-Station|1999–2013|[[Example FC]]}}
    {{Team-Station|2005|→ [[Example FC]] (interim)}}
    {{Team-Station|2006–2008|[[Example FC#Second team|Example FC II]]}}

Parsing is literal. A row whose years or club can't be read is counted, never guessed.
"""

import re
import sys
from collections import Counter
from dataclasses import dataclass

from .http import RateLimiter, WikimediaClient

_ZERO = re.compile(r"\{\{0(\|[^}]*)?\}\}")          # {{0}}, {{0|0000}}: invisible padding
_YEAR = re.compile(r"(?<!\d)(1[89]\d\d|20\d\d)(?!\d)")
_LINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]*))?\]\]")
_TABLE = re.compile(r"\|\s*trainer_tabelle\s*=(.*?)(?=\n\s*\|\s*[\wäöüß]+\s*=|\n\s*\}\})", re.DOTALL)
# A row counts as first-team head coach only when its note (the text beside the club link) is
# empty or one of these. Every other note, e.g. "(Co-Trainer)", "U19", "Am.", "(Verbindungstrainer)",
# makes it another role: the infobox uses too many spellings to list them all.
_HEAD_NOTE = re.compile(r"\(?\s*(interim\w*|spielertrainer|cheftrainer|chef-trainer|teamchef)?\s*\)?",
                        re.IGNORECASE)
# Link display text that names another team of the club, e.g. [[Example FC|Example FC Amateure]].
_OTHER_TEAM = re.compile(r"\bu-?\s?\d\d\b|jugend|junioren|amateure|reserve|\bii\b|\biii\b|\biv\b|frauen",
                         re.IGNORECASE)
_INTERIM = re.compile(r"interim", re.IGNORECASE)
_REF = re.compile(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", re.DOTALL)


@dataclass(frozen=True)
class Station:
    years: str          # raw years cell
    team: str           # raw team cell
    start: int | None   # None: years unreadable
    end: int | None     # None with a start: open-ended ("seit 2023", "2023–")
    target: str | None  # first link target, normalized, with any #section
    name: str           # plain team text without notes, for unlinked rows
    plain: str          # plain team text with notes
    note: str           # text beside the club: outside the link, or the parentheses of an unlinked row
    last: bool          # last row of the table (only a current job may be open-ended)


@dataclass(frozen=True)
class Page:
    title: str
    page_id: int
    rev_id: int
    text: str


@dataclass(frozen=True)
class CoachStation:
    """A readable station at one of our clubs, attributed to a Wikidata person."""
    person: str
    club: str
    start: int
    end: int | None
    role: str           # head | interim | other
    page: str
    page_id: int
    rev_id: int
    row: str


def normalize_title(title: str) -> str:
    t = re.sub(r"[\s_]+", " ", title.replace("&nbsp;", " ")).strip()
    return t[:1].upper() + t[1:]


def parse_years(cell: str) -> tuple[int, int | None] | None:
    """'1999–2013' -> (1999, 2013); '2016' -> (2016, 2016); 'seit 2023' or '2023–' -> (2023, None).
    None when no start year is readable or the cell is ambiguous ('', '–2000', '19??', '10/81–02/83',
    '2003–2004, 2006')."""
    text = _ZERO.sub("", cell).strip()
    years = [int(y) for y in _YEAR.findall(text)]
    if not years or "?" in text or re.match(r"^[–—-]", text) or re.search(r"[,;]|\bund\b", text):
        return None
    if re.match(r"(?i)^seit\b", text) or re.search(r"[–—-]\s*$", text):
        return years[0], None
    return years[0], years[-1]


def _templates(text: str, name: str) -> list[str]:
    """Bodies of every {{name|...}} in text, allowing nested templates."""
    out = []
    for m in re.finditer(r"\{\{\s*" + re.escape(name) + r"\s*\|", text):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            if text.startswith("{{", i):
                depth, i = depth + 1, i + 2
            elif text.startswith("}}", i):
                depth, i = depth - 1, i + 2
            else:
                i += 1
        if depth == 0:
            out.append(text[m.end():i - 2])
    return out


def _split_args(body: str) -> list[str]:
    args, depth, start, i = [], 0, 0, 0
    while i < len(body):
        pair = body[i:i + 2]
        if pair in ("{{", "[["):
            depth, i = depth + 1, i + 2
            continue
        if pair in ("}}", "]]"):
            depth, i = depth - 1, i + 2
            continue
        if body[i] == "|" and depth == 0:
            args.append(body[start:i])
            start = i + 1
        i += 1
    args.append(body[start:])
    return [a.strip() for a in args]


def _clean(text: str) -> str:
    """Wikitext cell -> plain text: footnotes, templates, tags and bold quotes removed."""
    text = re.sub(r"\{\{[^{}]*\}\}", " ", _REF.sub(" ", text))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>|'{2,}|&nbsp;", " ", text)).strip()


def parse_stations(wikitext: str) -> list[Station] | None:
    """Rows of the infobox coaching table, in order. None when the page has no such table."""
    m = _TABLE.search(wikitext)
    if m is None:
        return None
    bodies = _templates(_ZERO.sub("", m.group(1)), "Team-Station")
    out = []
    for n, body in enumerate(bodies):
        args = _split_args(body)
        years, team = args[0], (args[1] if len(args) > 1 else "")
        parsed = parse_years(years)
        link = _LINK.search(team)
        plain = _clean(_LINK.sub(lambda lk: lk.group(2) or lk.group(1), team))
        name = re.sub(r"\s*\([^)]*\)", "", plain).replace("→", "").strip()
        note = (_clean(_LINK.sub("", team, count=1)).replace("→", "").strip() if link
                else " ".join(re.findall(r"\([^)]*\)", plain)))
        out.append(Station(years=years, team=team, start=parsed[0] if parsed else None,
                           end=parsed[1] if parsed else None,
                           target=normalize_title(link.group(1)) if link else None,
                           name=name, plain=plain, note=note, last=n == len(bodies) - 1))
    return out


def match_club(station: Station, club_titles: dict[str, tuple[str, str | None]]) -> tuple[str, bool] | None:
    """(club, points at a section) when the station is at one of our clubs. A linked row is matched
    on its link target only; the plain name is used for unlinked rows. A section (an anchor in the
    link, or a redirect to a section such as the reserve team) is never the first team."""
    key = station.target if station.target else normalize_title(station.name)
    base, _, anchor = key.partition("#")
    hit = club_titles.get(base)
    if hit is None:
        return None
    club, redirect_section = hit
    return club, bool(anchor or redirect_section)


def station_role(station: Station, section: bool) -> str:
    if section or _OTHER_TEAM.search(station.plain) or not _HEAD_NOTE.fullmatch(station.note):
        return "other"
    return "interim" if _INTERIM.search(station.note) else "head"


def coach_stations(pages: dict[str, Page], title_to_person: dict[str, str],
                   club_titles: dict[str, tuple[str, str | None]], counts: Counter,
                   problems: list) -> list[CoachStation]:
    """Readable stations at our clubs for pages that map to a Wikidata person. Unreadable rows
    at our clubs go to `problems` as (page, years cell, team cell, reason)."""
    out = []
    for title, page in sorted(pages.items()):
        rows = parse_stations(page.text)
        if rows is None:
            counts["dewiki_pages_without_coaching_table"] += 1
            continue
        counts["dewiki_pages_with_coaching_table"] += 1
        for st in rows:
            hit = match_club(st, club_titles)
            if hit is None:
                continue
            club, section = hit
            role = station_role(st, section)
            counts[f"dewiki_rows_at_our_clubs:{role}"] += 1
            if st.start is None:
                problems.append((title, st.years, st.team, "years unreadable"))
                continue
            if st.end is None and not st.last:
                problems.append((title, st.years, st.team, "open-ended but not the current job"))
                continue
            if role == "interim" and st.end is not None and st.end - st.start >= 2:
                # Usually a sub-row marking a period with interim stints, not one spell.
                problems.append((title, st.years, st.team, "interim row spanning several years"))
                continue
            person = title_to_person.get(title)
            if person is None:
                counts["dewiki_rows_page_without_wikidata_person"] += 1
                problems.append((title, st.years, st.team, "page has no Wikidata item"))
                continue
            out.append(CoachStation(person=person, club=club, start=st.start, end=st.end, role=role,
                                    page=title, page_id=page.page_id, rev_id=page.rev_id,
                                    row=f"{st.years} | {st.team}"))
    return out


class DewikiApi:
    def __init__(self, client: WikimediaClient, endpoint: str, wikidata_endpoint: str,
                 titles_per_request: int, pause_seconds: float):
        self.client = client
        self.endpoint = endpoint
        self.wikidata_endpoint = wikidata_endpoint
        self.batch = titles_per_request
        self.limiter = RateLimiter(pause_seconds)

    def _query(self, params: dict, dump_name: str) -> list[dict]:
        """All result batches of an action=query request, following `continue`."""
        params = {"action": "query", "format": "json", "formatversion": "2", "maxlag": "5", **params}
        batches, cont = [], {}
        while True:
            data = self.client.get_json(self.endpoint, params={**params, **cont}, limiter=self.limiter,
                                        dump_name=dump_name)
            if "error" in data:
                raise RuntimeError(f"dewiki API error: {data['error']}")
            batches.append(data.get("query", {}))
            if "continue" not in data:
                return batches
            cont = data["continue"]

    def club_titles(self, clubs: dict[str, str]) -> dict[str, tuple[str, str | None]]:
        """{article or redirect title: (club, redirect section)} for our clubs' dewiki articles."""
        out = {normalize_title(t): (club, None) for club, t in clubs.items()}
        by_title = {normalize_title(t): club for club, t in clubs.items()}
        titles = sorted(by_title)
        for i in range(0, len(titles), self.batch):
            for q in self._query({"prop": "redirects", "titles": "|".join(titles[i:i + self.batch]),
                                  "rdprop": "title|fragment", "rdnamespace": "0", "rdlimit": "max"},
                                 "dewiki_club_redirects"):
                for page in q.get("pages", []):
                    club = by_title.get(page["title"])
                    for r in page.get("redirects", []) if club else []:
                        out.setdefault(r["title"], (club, r.get("fragment") or None))
        return out

    def category_members(self, category: str) -> list[str]:
        titles = []
        for q in self._query({"list": "categorymembers", "cmtitle": category, "cmnamespace": "0",
                              "cmlimit": "max"}, "dewiki_categories"):
            titles += [m["title"] for m in q.get("categorymembers", [])]
        return titles

    def pages(self, titles: list[str]) -> dict[str, Page]:
        """Current wikitext by requested title (redirects followed)."""
        out: dict[str, Page] = {}
        for i in range(0, len(titles), self.batch):
            chunk = titles[i:i + self.batch]
            print(f"  dewiki pages: {i + len(chunk)}/{len(titles)}", file=sys.stderr)
            for q in self._query({"prop": "revisions", "rvprop": "ids|content", "rvslots": "main",
                                  "titles": "|".join(chunk), "redirects": "1"}, "dewiki_pages"):
                renamed = {r["from"]: r["to"] for r in q.get("normalized", []) + q.get("redirects", [])}
                found = {p["title"]: p for p in q.get("pages", []) if p.get("revisions")}
                for t in chunk:
                    final = t
                    while final in renamed and renamed[final] != final:
                        final = renamed[final]
                    p = found.get(final)
                    if p:
                        rev = p["revisions"][0]
                        out[t] = Page(p["title"], p["pageid"], rev["revid"], rev["slots"]["main"]["content"])
        return out

    def wikidata_ids(self, titles: list[str]) -> dict[str, str]:
        """{dewiki title: Wikidata id} for titles that have an item."""
        out = {}
        for i in range(0, len(titles), self.batch):
            data = self.client.get_json(self.wikidata_endpoint, params={
                "action": "wbgetentities", "sites": "dewiki", "titles": "|".join(titles[i:i + self.batch]),
                "props": "sitelinks", "sitefilter": "dewiki", "format": "json", "maxlag": "5",
            }, limiter=self.limiter, dump_name="dewiki_wikidata_ids")
            if "error" in data:
                raise RuntimeError(f"wbgetentities error: {data['error']}")
            for e in data.get("entities", {}).values():
                title = e.get("sitelinks", {}).get("dewiki", {}).get("title")
                if title and "missing" not in e:
                    out[title] = e["id"]
        return out
