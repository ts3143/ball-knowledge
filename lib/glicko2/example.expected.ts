/** Output of `uv run python -m rating example` (fixed demo parameters, independent of tunables.toml), byte for
 * byte. sim/tests/test_example.py checks the Python side against this text; regenerate it if the example changes. */
export const PYTHON_EXAMPLE_OUTPUT = `(a) Glickman's worked example
    A player rated 1500 (RD 200, volatility 0.06) plays three opponents: a win against 1400 (RD 30),
    a loss against 1550 (RD 100) and a loss against 1700 (RD 300). tau = 0.5, standard weight 1.
                          ours   published
    new rating     1464.050671     1464.06
    new RD          151.516524      151.52
    volatility        0.059996     0.05999
    (volatility step converged in 2 iterations)

(b) A worked replay: one synthetic player, five dives in Competition A
    Rating and RD are shown before and after each dive. Every dive is one rating period for each scope
    it touches. Uncertain marker: RD above 110. A club rating is shown from 20 prompts.
    Prompt ratings here start at 1500 + 30 x log_recall_mass (demo values, not tuned).

  Dive 1, 2026-01-01: 130 points of 700
    Football (sport)   rating  1500.0 ->  1222.7   RD  350.0 ->  171.6   uncertain
    Competition A      rating  1500.0 ->  1222.7   RD  350.0 ->  171.6   uncertain
    Club A             not touched yet (no club prompt)

  Dive 2, 2026-01-02: 195 points of 700
    Football (sport)   rating  1222.7 ->  1220.2   RD  171.6 ->  135.0   uncertain
    Competition A      rating  1222.7 ->  1220.2   RD  171.6 ->  135.0   uncertain
    Club A             not touched yet (no club prompt)

  Dive 3, 2026-01-09: 335 points of 700   <- 6 dive dates skipped, so RD grows before the dive
    Football (sport)   rating  1220.2 ->  1292.2   RD  137.4 ->  116.5   uncertain
    Competition A      rating  1220.2 ->  1292.2   RD  137.4 ->  116.5   uncertain
    Club A             rating  1220.2 ->  1398.0   RD  250.0 ->  171.7   uncertain, rating hidden (6 of 20 prompts) (new: starts at its parent's rating, RD at least 250)

  Dive 4, 2026-01-10: 385 points of 700
    Football (sport)   rating  1292.2 ->  1350.7   RD  116.5 ->  101.9   settled
    Competition A      rating  1292.2 ->  1350.7   RD  116.5 ->  101.9   settled
    Club A             rating  1398.0 ->  1449.3   RD  171.7 ->  129.9   uncertain, rating hidden (13 of 20 prompts)

  Dive 5, 2026-01-11: 475 points of 700
    Football (sport)   rating  1350.7 ->  1414.7   RD  101.9 ->   91.2   settled
    Competition A      rating  1350.7 ->  1414.7   RD  101.9 ->   91.2   settled
    Club A             rating  1449.3 ->  1510.4   RD  129.9 ->  108.6   settled, rating shown

  A week after dive 5 (2026-01-18), RD if the player returned:
    Football (sport)   RD   91.2 ->   94.7   settled
    Competition A      RD   91.2 ->   94.7   settled
    Club A             RD  108.6 ->  111.5   uncertain, rating shown
`;
