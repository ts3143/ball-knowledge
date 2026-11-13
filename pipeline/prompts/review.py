"""T's approval of prompts (hard rule 7: T approves every prompt before it ships).

    python -m prompts review [--template T] [--limit N]    # interactive: one prompt at a time
    python -m prompts approve 2801 2802 [--note "..."]      # non-interactive
    python -m prompts reject 2803 --note "ambiguous"

Only a gate_passed prompt can be approved. Every decision is committed at once, so quitting loses nothing.
Approving does not schedule: `python -m dives schedule` does. Prompts the gates failed are never shown.
"""

import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TextIO

import psycopg
from psycopg.rows import dict_row

from rarity.model import WEAK_FLAG

from .difficulty import seed_difficulty, tercile_bands
from .gates import APPROVED, DECIDED_STATUSES, PASSED_STATUSES, REJECTED

WIDTH = 78
NAME_WIDTH = 28
ROWS = 5  # top and bottom answers shown


@dataclass
class ReviewPrompt:
    id: int
    template: str
    text: str
    status: str
    club_names: list[str]
    answer_count: int
    tier_counts: list[int]
    seed: float | None
    band: str | None
    answers: list[dict]  # name, share, tier, points, flags; most likely pick first
    pool_size: int = 0
    weak: int = field(init=False)

    def __post_init__(self):
        self.weak = sum(WEAK_FLAG in a["flags"] for a in self.answers)


@dataclass(frozen=True)
class Decision:
    prompt_id: int
    status: str  # the new status
    note: str | None = None
    text: str | None = None  # the edited wording, if it changed


class ReviewError(Exception):
    """A decision that cannot be applied; nothing was changed."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def competition_id(conn: psycopg.Connection, qid: str) -> int:
    with conn.transaction():
        row = conn.execute("select id from entities where wikidata_id = %s and type = 'competition'",
                           (qid,)).fetchone()
    if row is None:
        raise ReviewError(f"no competition entity {qid}; run the ingest first")
    return row[0] if isinstance(row, tuple) else row["id"]


def seed_pool(cur, tunables: dict, competition: int) -> dict[int, float]:
    """prompt id -> seed difficulty, for every prompt of the competition that passed the gates (the pool the
    bands are cut from)."""
    cur.execute("select id, rarity, gate_results from prompts where status = any(%s) and competition_id = %s",
                (list(PASSED_STATUSES), competition))
    seeds = {r["id"]: seed_difficulty(tunables, r["rarity"], r["gate_results"]) for r in cur.fetchall()}
    return {pid: s for pid, s in seeds.items() if s is not None}


def queue(cur, competition: int, template_ids: list[str] | None = None, limit: int | None = None) -> list[int]:
    """Ids of the gate_passed prompts to review: templates take turns (by id within a template), so a --limit
    shows a mix of templates rather than only the first."""
    cur.execute("select id, template from prompts where status = 'gate_passed' and competition_id = %s"
                " and (%s::text[] is null or template = any(%s)) order by template, id",
                (competition, template_ids, template_ids))
    by_template: dict[str, list[int]] = defaultdict(list)
    for r in cur.fetchall():
        by_template[r["template"]].append(r["id"])
    lanes = [by_template[t] for t in sorted(by_template)]
    ordered = [lane[i] for i in range(max(map(len, lanes), default=0)) for lane in lanes if i < len(lane)]
    return ordered if limit is None else ordered[:limit]


def load_prompt(cur, prompt_id: int, seeds: dict[int, float], bands: dict[int, str]) -> ReviewPrompt:
    cur.execute("select id, template, text, status, club_ids, answer_count, gate_results from prompts where id = %s",
                (prompt_id,))
    p = cur.fetchone()
    if p is None:
        raise ReviewError(f"prompt {prompt_id} does not exist")
    cur.execute("select id, canonical_name from entities where id = any(%s)", (p["club_ids"],))
    names = {r["id"]: r["canonical_name"] for r in cur.fetchall()}
    cur.execute("select e.canonical_name as name, a.predicted_share as share, a.tier, a.points, a.flags"
                " from prompt_answers a join entities e on e.id = a.entity_id where a.prompt_id = %s"
                " order by a.predicted_share desc nulls last, e.canonical_name", (prompt_id,))
    tiers = p["gate_results"].get("tiers")
    return ReviewPrompt(
        id=p["id"], template=p["template"], text=p["text"], status=p["status"],
        club_names=[names[c] for c in p["club_ids"]], answer_count=p["answer_count"],
        tier_counts=list(tiers["tier_counts"]) if isinstance(tiers, dict) else [],
        seed=seeds.get(prompt_id), band=bands.get(prompt_id), answers=cur.fetchall(), pool_size=len(seeds))


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _short(name: str) -> str:
    return name if len(name) <= NAME_WIDTH else name[:NAME_WIDTH - 1] + "…"


def _answer_line(rank: int, a: dict, tier_names: list[str]) -> str:
    return (f"  {rank:>3}. {_short(a['name']):<{NAME_WIDTH}} {tier_names[a['tier'] - 1]:<14} "
            f"{a['points']:>3} pts  {100 * a['share']:>5.1f}%" + ("  weak" if WEAK_FLAG in a["flags"] else ""))


def render(p: ReviewPrompt, position: int, total: int, tier_names: list[str]) -> str:
    """One prompt as a screen of at most WIDTH columns."""
    lines = ["=" * WIDTH,
             f"[{position} of {total}]  prompt {p.id}  {p.template}  ({p.status})",
             "", p.text, ""]
    if p.club_names:
        lines.append("clubs: " + ", ".join(p.club_names))
    lines.append(f"answers: {p.answer_count}")
    if p.tier_counts:
        lines.append("tiers:   " + "  ".join(f"{n} {c}" for n, c in zip(tier_names, p.tier_counts)))
    if p.seed is not None:
        lines.append(f"seed difficulty: {p.seed:.0f} (higher = harder; {p.band} third of {p.pool_size} prompts)")
    else:
        lines.append("seed difficulty: not available (missing rarity features)")
    lines.append(f"weak answers: {p.weak} of {p.answer_count} (uncertain dates; capped at Real One)")
    n = len(p.answers)
    lines += ["", f"most likely answers (top {min(ROWS, n)} by predicted share)"]
    lines += [_answer_line(i + 1, a, tier_names) for i, a in enumerate(p.answers[:ROWS])]
    if n > ROWS:
        lines += ["", f"least likely answers (bottom {min(ROWS, n - ROWS)})"]
        first = max(ROWS, n - ROWS)
        lines += [_answer_line(first + i + 1, a, tier_names) for i, a in enumerate(p.answers[first:])]
    return "\n".join(lines)


def render_all_answers(p: ReviewPrompt, tier_names: list[str]) -> str:
    """Every answer of the prompt, most likely first: rank, name, tier, points, predicted share, weak flag."""
    lines = [f"all {len(p.answers)} answers of prompt {p.id}, by predicted share"]
    lines += [_answer_line(i, a, tier_names) for i, a in enumerate(p.answers, 1)]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Decisions (each one is committed at once)
# ---------------------------------------------------------------------------

def decide(conn: psycopg.Connection, decisions: list[Decision]) -> None:
    """Applies the decisions in one transaction, or none: a ReviewError names every prompt that cannot take its
    decision. approve needs gate_passed; reject needs gate_passed, or an approved prompt no dive uses yet."""
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        problems = []
        for d in decisions:
            cur.execute("select status, text, exists (select 1 from dive_prompts where prompt_id = prompts.id)"
                        " as scheduled from prompts where id = %s for update", (d.prompt_id,))
            row = cur.fetchone()
            if row is None:
                problems.append(f"prompt {d.prompt_id} does not exist")
            elif row["scheduled"]:
                problems.append(f"prompt {d.prompt_id} is in a dive; `dives unschedule` that date first")
            elif d.status == APPROVED and row["status"] != "gate_passed":
                problems.append(f"prompt {d.prompt_id} is {row['status']}: only gate_passed prompts can be approved")
            elif d.status == REJECTED and row["status"] not in ("gate_passed", APPROVED):
                problems.append(f"prompt {d.prompt_id} is {row['status']}: nothing to reject")
            else:
                note = d.note
                if d.text is not None and d.text != row["text"]:
                    edited = f"wording edited; was: {row['text']}"
                    note = f"{note}\n{edited}" if note else edited
                cur.execute("update prompts set status = %s, reviewed_at = now(), review_note = %s,"
                            " text = coalesce(%s, text), updated_at = now() where id = %s",
                            (d.status, note, d.text, d.prompt_id))
        if problems:
            raise ReviewError("\n".join(problems))  # leaves the transaction block: everything rolls back


def decide_many(conn: psycopg.Connection, ids: list[int], status: str, note: str | None) -> None:
    if status == REJECTED and not (note or "").strip():
        raise ReviewError("reject needs a --note saying why")
    if status not in DECIDED_STATUSES:
        raise ValueError(status)
    decide(conn, [Decision(i, status, note) for i in dict.fromkeys(ids)])


# ---------------------------------------------------------------------------
# The interactive loop
# ---------------------------------------------------------------------------

MENU = "[a]pprove  [r]eject  [e]dit text + approve  [l]ist all answers  [s]kip  [q]uit > "


def review_loop(conn: psycopg.Connection, tunables: dict, competition: int,
                template_ids: list[str] | None = None, limit: int | None = None, input_fn: Callable[[str], str] = input,
                out: TextIO = sys.stdout) -> dict[str, int]:
    """Shows the gate_passed prompts one at a time and applies T's choices. Returns how many were approved,
    rejected, edited (approved with new wording), skipped, and how many were left unseen after quitting."""
    tier_names = tunables["tiers"]["names"]
    counts = {"approved": 0, "rejected": 0, "edited": 0, "skipped": 0, "unseen": 0}

    def say(text: str = "") -> None:
        print(text, file=out)

    def ask(prompt: str) -> str | None:
        """None at end of input (Ctrl-D) or Ctrl-C: treated as quit."""
        try:
            return input_fn(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            say()
            return None

    # Reads run in short transactions (a block commits on exit), so none stays open while T thinks.
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        ids = queue(cur, competition, template_ids, limit)
        seeds = seed_pool(cur, tunables, competition)
    bands = tercile_bands(seeds)
    if not ids:
        say("No gate_passed prompts waiting for review.")
        return counts
    say(f"{len(ids)} prompts to review. Decisions are saved as you go.")
    for position, pid in enumerate(ids, 1):
        with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            prompt = load_prompt(cur, pid, seeds, bands)
        say()
        say(render(prompt, position, len(ids), tier_names))
        say()
        while True:
            choice = ask(MENU)
            if choice is None or choice.lower() in ("q", "quit"):
                counts["unseen"] = len(ids) - position + 1
                return counts
            choice = choice.lower()
            try:
                if choice in ("a", "approve"):
                    decide(conn, [Decision(pid, APPROVED)])
                    counts["approved"] += 1
                elif choice in ("r", "reject"):
                    note = ask("why reject? (empty = back) > ")
                    if not note:
                        continue
                    decide(conn, [Decision(pid, REJECTED, note)])
                    counts["rejected"] += 1
                elif choice in ("e", "edit"):
                    say(f"current: {prompt.text}")
                    text = ask("new wording (empty = back) > ")
                    if not text:
                        continue
                    decide(conn, [Decision(pid, APPROVED, text=text)])
                    counts["approved"] += 1
                    counts["edited"] += 1
                elif choice in ("l", "list"):
                    say(render_all_answers(prompt, tier_names))
                    say()
                    continue  # nothing changes; ask again about the same prompt
                elif choice in ("s", "skip"):
                    counts["skipped"] += 1
                else:
                    say("Please answer a, r, e, l, s or q.")
                    continue
            except ReviewError as e:
                say(f"Not saved: {e}")
                counts["skipped"] += 1
            break
    return counts


def print_counts(counts: dict[str, int], out: TextIO = sys.stdout) -> None:
    print(f"approved {counts['approved']} (of which edited {counts['edited']}), rejected {counts['rejected']}, "
          f"skipped {counts['skipped']}, not reached {counts['unseen']}", file=out)
