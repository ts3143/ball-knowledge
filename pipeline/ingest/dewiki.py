"""German Wikipedia infobox tables: the second source for coach spells and player stints.

Exception to "Wikidata + pageviews only", approved by T on 2026-11-11, for coach spells
(`trainer_tabelle`) and player stints (`vereine_tabelle`, `jugendvereine_tabelle`). Every fact or
value taken from them is tagged source 'dewiki'.
Wikidata often links a coach to a club without dates, or not at all. The `trainer_tabelle`
of dewiki's "Infobox Fußballspieler" lists each coaching station with years and a role note:

    {{Team-Station|1999–2013|[[Example FC]]}}
    {{Team-Station|2005|→ [[Example FC]] (interim)}}
    {{Team-Station|2006–2008|[[Example FC#Second team|Example FC II]]}}

The player tables use the same template with a third argument, "apps (goals)":

    {{Team-Station|2004–2005|[[Example FC]]|34 {{0}}(3)}}
    {{Team-Station|2009|→ [[Example FC]] (Leihe)|2 {{0}}(0)}}

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
COACH_TABLE = "trainer_tabelle"
PLAYER_TABLE = "vereine_tabelle"
YOUTH_TABLE = "jugendvereine_tabelle"


def _table_regex(name: str) -> re.Pattern:
    return re.compile(r"\|\s*" + re.escape(name) + r"\s*=(.*?)(?=\n\s*\|\s*[\wäöüß]+\s*=|\n\s*\}\})", re.DOTALL)


# A row counts as first-team head coach only when its note (the text beside the club link) is
# empty or one of these. Every other note, e.g. "(Co-Trainer)", "U19", "Am.", "(Verbindungstrainer)",
# makes it another role: the infobox uses too many spellings to list them all.
_HEAD_NOTE = re.compile(r"\(?\s*(interim\w*|spielertrainer|cheftrainer|chef-trainer|teamchef)?\s*\)?",
                        re.IGNORECASE)
# Link display text that names another team of the club, e.g. [[Example FC|Example FC Amateure]].
_AMATEUR = r"\bam(?:at)?\.|\(\s*am(?:at)?\.?\s*\)"       # "Am.", "Amat.", "(Am)": dewiki's Amateure
_OTHER_TEAM = re.compile(r"\bu-?\s?\d\d\b|jugend|junioren|amateur|reserve|\bii\b|\biii\b|\biv\b|frauen|"
                         + _AMATEUR, re.IGNORECASE)
_INTERIM = re.compile(r"interim", re.IGNORECASE)
_NAMED_ARG = re.compile(r"^(\w+)\s*=(.*)$", re.DOTALL)   # leihe=1, farmteam=ja
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
    apps_cell: str = ""  # first positional argument after the team in the player tables, e.g. "68 (12)"
    loan: bool = False   # named argument leihe=1 / leihe=ja


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
    '2003–2004, 2006', '2006–01/07', a reversed range). Footnotes are ignored."""
    text = _clean(_ZERO.sub("", cell))
    years = [int(y) for y in _YEAR.findall(text)]
    if not years or "?" in text or re.match(r"^[–—-]", text) or re.search(r"[,;]|\bund\b", text):
        return None
    if re.match(r"(?i)^seit\b", text) or re.search(r"[–—-]\s*$", text):
        return years[0], None
    tail = re.split(r"[–—-]", text, maxsplit=1)[1] if re.search(r"[–—-]", text) else None
    if tail is not None and not _YEAR.search(tail):
        return None         # an end that isn't a four-digit year: "01/07", "07"
    if years[-1] < years[0]:
        return None
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


def parse_stations(wikitext: str, table: str = COACH_TABLE) -> list[Station] | None:
    """Rows of one infobox table, in order. None when the page has no such table."""
    m = _table_regex(table).search(wikitext)
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
        positional = [a for a in args[2:] if not _NAMED_ARG.match(a)]
        named = {m.group(1).lower(): m.group(2).strip() for a in args[2:] if (m := _NAMED_ARG.match(a))}
        out.append(Station(years=years, team=team, start=parsed[0] if parsed else None,
                           end=parsed[1] if parsed else None,
                           target=normalize_title(link.group(1)) if link else None,
                           name=name, plain=plain, note=note, last=n == len(bodies) - 1,
                           apps_cell=_clean(positional[0]) if positional else "",
                           loan=named.get("leihe", "").lower() not in ("", "0", "nein")))
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


SENIOR_ROLES = ("first_team", "loan")
_LOAN = re.compile(r"→|\bleihe\b|leihweise|ausgeliehen", re.IGNORECASE)
_YOUTH_TEAM = re.compile(r"\bu-?\s?1\d\b|jugend|junioren|nachwuchs", re.IGNORECASE)
_RESERVE_TEAM = re.compile(r"\bu-?\s?2\d\b|amateur|reserve|\bii\b|\biii\b|\biv\b|frauen|zweite|" + _AMATEUR,
                           re.IGNORECASE)
# A section anchor that names the first team; any other anchor that isn't a youth/reserve team is unknown.
_FIRST_TEAM_ANCHOR = re.compile(r"erste|profi|lizenz|herren", re.IGNORECASE)
# A team name that is one of our clubs plus a suffix naming another team: "Example FC II",
# "Example FC U-23". Such teams have their own article, so they are not in club_titles.
_TEAM_SUFFIX = re.compile(r"\s+(?:(?:ii|iii|iv|amateure|reserve|u-?\s?\d\d|[a-e]-jugend|jugend|junioren)\b|"
                          r"am(?:at)?\.|\(\s*am(?:at)?\.?\s*\)).*$", re.IGNORECASE)
_APPS = re.compile(r"^(\d+)\s*(?:\((\d+)\))?$")


@dataclass(frozen=True)
class PlayerRow:
    """A readable row of a player's career table at one of our clubs."""
    person: str
    club: str
    start: int
    end: int | None
    role: str           # first_team | loan | reserve | youth
    apps: int | None
    goals: int | None
    page: str
    page_id: int
    rev_id: int
    row: str


def parse_apps(cell: str) -> tuple[int | None, int | None]:
    """'215 (126)' -> (215, 126); '68 {{0}}(42)' too (padding is stripped before); '' -> (None, None)."""
    m = _APPS.match(cell.strip())
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2)) if m.group(2) is not None else None


def _strip_team_suffix(title: str) -> str:
    base, _, anchor = title.partition("#")
    return _TEAM_SUFFIX.sub("", base).strip() if not anchor else base


def player_role(station: Station, club_titles: dict[str, tuple[str, str | None]],
                youth_table: bool) -> tuple[str, str] | None:
    """(club, role) when the row is at one of our clubs (any of its teams), else None.

    Roles: first_team, loan, reserve, youth, or unknown (a section anchor we can't place).
    A row of the youth table is youth. Text (display, note, link target, anchor) naming a youth team
    (U19, Jugend, Junioren) is youth; one naming a reserve team (II, Amateure, Am., Amat., U23) is
    reserve, as is a team named like one of ours plus such a suffix. A section anchor that names the
    first team (Erste Mannschaft, Profis) is the first team; any other anchor is unknown. A redirect
    to a section is reserve. An arrow, "(Leihe)" or leihe=1 marks a loan, which counts as playing
    for the club."""
    key = station.target if station.target else normalize_title(station.name)
    hit = match_club(station, club_titles)
    by_suffix = False
    if hit is None:
        base = _strip_team_suffix(key)
        if base == key.partition("#")[0] or (own := club_titles.get(base)) is None:
            return None
        hit, by_suffix = (own[0], True), True
    club, section = hit
    text = f"{station.plain} {key}"
    if youth_table or _YOUTH_TEAM.search(text):
        return club, "youth"
    if by_suffix or _RESERVE_TEAM.search(text):
        return club, "reserve"
    anchor = key.partition("#")[2]
    if anchor and not _FIRST_TEAM_ANCHOR.search(anchor):
        return club, "unknown"
    if section and not anchor:      # a redirect to a section of the club article
        return club, "reserve"
    return club, "loan" if _is_loan(station) else "first_team"


def _is_loan(station: Station) -> bool:
    return station.loan or bool(_LOAN.search(station.team + " " + station.plain))


def current_club_rows(rows: list[Station], hits: list[tuple[str, str] | None]) -> list[bool]:
    """For each row: may it be open-ended ("2019–")? Yes when every later row of the table is another
    team of the same club (reserve, youth, II, section) or a loan: the player is still at the club,
    the infobox just lists the second team or a loan-out after it. A later senior row anywhere else
    means the open end is stale."""
    ok = []
    for i, st in enumerate(rows):
        club = hits[i][0] if hits[i] else None
        ok.append(all((hits[j] is not None and hits[j][0] == club and hits[j][1] in ("reserve", "youth"))
                      or _is_loan(rows[j]) for j in range(i + 1, len(rows))))
    return ok


def player_rows(pages: dict[str, Page], title_to_person: dict[str, str],
                club_titles: dict[str, tuple[str, str | None]], counts: Counter,
                problems: list, unreadable: set | None = None) -> list[PlayerRow]:
    """Readable rows at our clubs from the career tables of the pages whose title maps to a person.
    Unreadable senior rows go to `problems` as (page, years cell, team cell, reason) and their
    (person, club) to `unreadable`; unreadable youth and reserve rows are only counted, since they
    can't produce a fact."""
    out = []
    for requested, page in sorted(pages.items()):
        person = title_to_person.get(requested)
        if person is None:
            continue
        tables = [(parse_stations(page.text, PLAYER_TABLE), False), (parse_stations(page.text, YOUTH_TABLE), True)]
        if tables[0][0] is None and tables[1][0] is None:
            counts["dewiki_player_pages_without_career_table"] += 1
            continue
        counts["dewiki_player_pages_with_career_table"] += 1
        # A senior table with no readable rows (ABFRAGE_WIKIDATA, empty) hides that person's first-team
        # stints: every club the youth table names counts as having an unreadable senior row.
        senior_unreadable = not tables[0][0]
        if senior_unreadable:
            counts["dewiki_player_pages_senior_table_not_literal"] += 1
        for rows, youth_table in tables:
            rows = rows or []
            hits = [player_role(st, club_titles, youth_table) for st in rows]
            current = current_club_rows(rows, hits)
            for st, hit, is_current in zip(rows, hits, current, strict=True):
                if hit is None:
                    continue
                club, role = hit
                counts[f"dewiki_player_rows_at_our_clubs:{role}"] += 1
                if senior_unreadable and unreadable is not None:
                    unreadable.add((person, club))
                senior = role in SENIOR_ROLES or role == "unknown"
                reason = ("section anchor not recognized" if role == "unknown" else
                          "years unreadable" if st.start is None else
                          "open-ended but not the current club" if st.end is None and not is_current else None)
                if reason:
                    if senior:
                        problems.append((requested, st.years, st.team, reason))
                        if unreadable is not None:
                            unreadable.add((person, club))
                    else:
                        counts["dewiki_player_rows_unreadable_non_senior"] += 1
                    continue
                apps, goals = parse_apps(st.apps_cell)
                if st.apps_cell and apps is None:
                    counts["dewiki_player_apps_cells_unreadable"] += 1
                out.append(PlayerRow(person=person, club=club, start=st.start, end=st.end, role=role,
                                     apps=apps, goals=goals, page=page.title, page_id=page.page_id,
                                     rev_id=page.rev_id, row=f"{st.years} | {st.team} | {st.apps_cell}"))
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
