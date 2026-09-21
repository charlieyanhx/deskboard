# deskboard — design

## One rule

Everything downstream of the bus is a pure function of events. No module reads the wall
clock for a number; replay pacing only delays delivery. Consequences that are tested:

- the same session file produces the same `Book.state_hash()` on every run, at 0× or 10⁶×;
- the committed `data/demo/STATE_HASH` is reproduced by CI on Linux from the committed
  parquet, which `deskboard record` regenerates row-for-row (the file's bytes are not
  stable: the parquet footer embeds the pandas and pyarrow versions, and pandas 3.0.6
  changed them with no change in the rows).

Measured, not assumed: the parquet's rows are identical across macOS and Linux, but the raw
Greeks are not — libm differences in `norm.cdf` and Brent move the last bits. The hash
therefore rounds to 1e-6 before hashing; within a platform the state is bit-identical,
across platforms it is identical to a millionth of a dollar. The first CI run caught this.

## Bus

`asyncio`, six topics (`quote, trade, fill, position, alert, clock`), handlers called
synchronously in subscription order on the publishing task (async handlers awaited in
order). Per-event dispatch latency is recorded; the budget is p99 < 10 ms and the test
enforces it. Measured on the demo session on a laptop: p50 ≈ 0.7 ms, p99 ≈ 2.5 ms.

## Book

Positions → legs; every option quote is inverted to an implied vol and re-Greeked at the
current spot. Attribution runs between consecutive marks of a leg with Greeks at the old
mark (delta, ½-gamma, vega, theta); the residual is whatever those do not explain and is
reported as a component, never folded into another. Fills book an execution component
(mid at fill − fill price). Identity, tested per position and for the book:

    realized_pnl == delta + gamma + vega + theta + execution + residual

Dollar Greeks: Δ·S·q·mult, ½Γ·(1 % S)²·q·mult, ν·0.01·q·mult, Θ·q·mult per day.

A position arriving mid-session with fills is marked at the last quote before the fill
(the book caches the last quote of every contract it sees, held or not), so its P&L clock
starts at the fill and its execution cost is booked at once.

## Pricer

Black-Scholes-Merton with dividend yield; checked against closed-form values to 1e-8 (the
S=K=100/5%/20%/1y case and Hull's worked S=42/K=40 example), put-call parity, and central
finite differences. It is an interface: `price`, `greeks`,
`implied_vol`. An arbitrage-free surface pricer replaces it without touching attribution.

## Feeds

`ReplayFeed` publishes rows of `(ts, topic, payload)` from parquet/CSV — the same shape a
live connector publishes. `synth.record()` writes one day of 1-minute quotes for the demo
blotter from a skewed, drifting vol surface, with an outage and one intraday fill. A live
underlying feed (Polygon) is v0.3; the dashboard never submits orders.

## UI

Panel (`FastListTemplate`, Tabulator, bokeh vbar). Chosen over Streamlit and Dash because
the server runs on the same asyncio loop as the bus, so the feed is a task, not a poll,
and the UI is a periodic read of `Book.snapshot()`. The UI has no influence on any number;
a textual TUI would be a second consumer of the same snapshot.

## Scenario ladder

`engine/scenarios.ladder` reprices every leg with a valid mark by Black-Scholes at the
shocked spot and implied vol (stock legs linearly), optionally rolled forward by `days`.
P&L is measured against the model price at the current mark, not the mid, so the zero cell
is exactly 0 and the small-shock cells reproduce the dollar Greeks (tested: ±1 % spot vs
Δ$ and Γ$, ±1 vol vs ν$, a short roll's rate vs Θ$). Legs without a valid implied vol are
listed in `missing` — never priced at zero (the expired-leg-at-zero lesson).

## Execution page

`Book.fills` keeps one row per fill: price, mid and half-spread at the moment of the fill,
cost in $ per contract (positive = paid more than mid) and as a fraction of the half-spread
— the two units [tcakit](https://github.com/charlieyanhx/tcakit) reports for option combos,
so a desk reading both sees the same number. Σ(cost × qty) equals −`execution` in the
attribution (tested). Benchmarks that need the whole order lifecycle (arrival, interval
VWAP, reversion) are tcakit's job, not the dashboard's.

## Limits and alerts

`LimitEngine` evaluates plain-data rules (`scope ∈ {book, position}`, metric, max/min,
bound) against every snapshot after every event and publishes an `alert` event only when a
(rule, target) changes state. Clearing needs the value 5 % of |bound| back inside
(hysteresis). The alert carries value, bound and a sentence — the thing a trader acts on.
The engine reads the snapshot and never writes to the book, so the state hash is unchanged
by any rule set.

## Telegram

`alerts/telegram.py`: one `TelegramBot` subscribed to `alert`, sending via the Bot API
(`sendMessage`, HTML) and long-polling `getUpdates` on the server's loop. Commands are
answered from the current snapshot; messages from any chat other than the configured id
are ignored; messages that arrived while the bot was down are skipped. Every API failure
becomes one `TelegramError` line carrying Telegram's `description` and a hint naming the
env var to check, with the token never echoed (the URL carries it, so httpx's own message
is not shown). A failure inside the bus chain is logged and swallowed — the book must not
stop because a phone is unreachable. The API is an injected protocol so the bot is tested
with a fake — no network in tests, no token anywhere in the repo.

## One desk per process

`serve()` builds the desk, the feed and the bot once and starts them on the server's
asyncio loop via `pn.serve(start=False)` + `io_loop.add_callback`; each browser session
only attaches widgets and a periodic `snapshot()` read. Before this change a session
built its own desk, so nothing ran until a tab opened, a second tab replayed the day again
and pushed every alert twice, and two pollers on one token produced 409s. The review
caught it; the fix is the reason the README screenshot could be taken at the close.

## Health page

`engine/health.py` takes a daily P&L history (`date,pnl[,backtest]`, dollars) and reports
what a desk asks about a strategy rather than a book: Sharpe on the full business-day
calendar (inactive days are $0 — the active-day Sharpe of a one-day-in-five strategy
overstates by ~√5, tested), rolling 63- and 252-day windows, drawdown from the running
high-water mark, and whether live is tracking the backtest.

The tracking test is a one-sided CUSUM (Page 1954) on the live-minus-backtest difference,
normalised by that difference's own daily std, allowance `k = 0.5`. The threshold is
`h = 8`, not the textbook 5: simulated on 500 business days of Gaussian noise, `h = 5`
flags 41 % of clean histories and `h = 8` flags 2.7 %, while a 0.8-std/day fade is still
caught at a median delay of 23 days (2,000 paths; the test reproduces it on 300). A flag
is the date the CUSUM crossed — a reason to look, not a verdict. The UI appends today's
live P&L as a provisional last day so the page moves with the session.

## Not yet

Live underlying feed, recorded GIF (v0.3); TUI, Grafana export (v0.4).
