# Ball Knowledge

A daily sports trivia game. Each dive is 7 "Name a…" prompts with 25 seconds each; rarer valid answers score more,
and every fan gets a Glicko-2 rating ("BK") per club, competition and sport.

Status: in development. Bundesliga only so far; you can play a dive anonymously in a bare UI. Accounts, saved
ratings and the archive come next.

## How it works

- Every answer set is a database query over facts ingested from Wikidata (plus German Wikipedia infobox tables as a
  second source). No LLM writes answers.
- A rarity model (Wikipedia pageviews, tie to the club, recency) predicts how often each answer gets picked and
  assigns tiers.
- The server serves prompts, enforces the timer, matches answers and scores them. Answer sets never reach the browser.

## Layout

```
app/        Next.js app (App Router): pages and API routes
lib/        TypeScript: answer matching, scoring, Glicko-2, dive service, auth, db
pipeline/   Python: ingest/, prompts/, rarity/, dives/ (scheduling)
sim/        Python: reference Glicko-2 engine and rating simulation
supabase/   Postgres migrations and local config
tunables.toml   every tunable (timer, tiers, points, Glicko constants, thresholds)
```

## Run it locally

Needs Node 24, Python 3.12 with [uv](https://docs.astral.sh/uv/), and the
[Supabase CLI](https://supabase.com/docs/guides/local-development).

```sh
supabase start                              # local Postgres + auth, applies the migrations
cp .env.example .env                        # then fill in the values it describes

cd pipeline && uv sync
uv run python -m ingest bundesliga          # Wikidata + Wikipedia -> entities, aliases, facts
uv run python -m prompts generate bundesliga
uv run python -m dives open-now bundesliga  # a test dive that is open right now
cd ..

npm install
npm run db:app-role                         # once: sets the app role's password from .env
npm run dev                                 # http://localhost:3000
```

Throw test dives away again with `uv run python -m dives purge-test bundesliga --all`.

## Tests

```sh
npm test && npm run typecheck               # TypeScript (npm run test:db for the database tests)
(cd pipeline && uv run pytest)
(cd sim && uv run pytest)
```
