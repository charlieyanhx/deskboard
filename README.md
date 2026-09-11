# deskboard

[![ci](https://github.com/charlieyanhx/deskboard/actions/workflows/ci.yml/badge.svg)](https://github.com/charlieyanhx/deskboard/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![license](https://img.shields.io/badge/license-MIT-green)

A real-time options risk and P&L dashboard with a **deterministic replay mode**: an asyncio
event bus, Black-Scholes Greeks and dollar-Greeks per leg, P&L attribution with an exact
identity, and a Panel UI that only reads engine state. Same session file → same screens,
byte for byte — that is the test.

```
quotes / fills / positions ─► bus ─► Book (Greeks, dollar-Greeks, attribution) ─► snapshot ─► UI
        ▲                                  pure function of events                    │
   replay file (N× speed)                                                    Risk · P&L · Legs · Feed
   or live connector
```

## Run it

```bash
pip install -e ".[dev]"
pytest -q                  # 14 tests: Hull values, parity, finite differences, bus order,
                           #           replay determinism, attribution identity, latency budget
deskboard record           # regenerate the demo session (3,503 events, seeded)
deskboard replay           # headless: totals, bus latency, state hash
deskboard serve --speed 60 # http://localhost:5006 — one trading day in ~7 minutes
```

`deskboard replay` on the demo prints, among others:

```
pnl 411.70   pnl_delta 426.68   pnl_gamma -58.29   pnl_vega 56.61   pnl_theta 7.10
pnl_execution -13.50   pnl_residual -6.90   identity_gap 0.0
bus latency ms: p50 0.73  p99 2.49
state hash: 6ed65c7439cea855…
```

Read it as a trader would: today's +$412 is delta (+$427) less short-gamma bleed on the
afternoon air-pocket (−$58), plus vega from the post-lunch vol bump (+$57), a little
theta, the spread paid opening `A-0615` at 11:00 (−$13.50 — the execution line), and a
residual of −$6.90 that the Greeks do not explain and the screen does not hide.

## What it shows

| Page | Content |
|---|---|
| **Risk** | P&L today, net Δ$, Γ$ per 1 %, ν$ per vol point, Θ$ per day, residual, identity gap (must read 0.0000), spot; positions table with per-position Greeks and attribution |
| **P&L** | attribution bars: delta / gamma / vega / theta / execution / residual |
| **Legs** | per-contract mid, implied vol, Greeks, P&L |
| **Feed** | replay progress and bus dispatch latency p50 / p99 / max |

Numbers, and a reason for each number. No AI-insight widgets.

## Design rules (all tested)

- **Determinism**: replaying the same file gives the same `state_hash()`, at any speed. CI regenerates the demo session byte-identically on Linux and checks the committed hash (rounded to 1e-6: raw Greeks differ across platforms in the last bits — measured, see DESIGN.md).
- **Attribution identity**: `realized == delta + gamma + vega + theta + execution + residual` for every position and the book, to 1e-9; the residual is a component, not a plug.
- **Latency budget**: bus dispatch p99 < 10 ms, measured every run (demo: p50 0.7 ms, p99 2.5 ms).
- **Pricer is an interface**: BSM today (Hull-checked), a surface pricer later, attribution untouched.
- **Never submits orders**; positions come from a blotter file — the demo one, or a live bot's state file with the same `{positions: [{pos_id, legs: [...]}]}` shape (`--blotter`).

## What is where

```
src/deskboard/
  bus.py              asyncio bus; ordered dispatch; latency histogram
  engine/greeks.py    BSM price / Greeks / implied vol (Brent)
  engine/book.py      positions, marks, dollar-Greeks, attribution, snapshot, state hash
  feeds/replay.py     parquet/CSV → bus at N× speed
  feeds/synth.py      seeded demo session recorder + demo blotter
  feeds/blotter.py    positions from a file (demo or live-state shape)
  ui/app.py           Panel app: Risk / P&L / Legs / Feed
  cli.py              record · replay · serve
data/demo/            session parquet, blotter.json, STATE_HASH
docs/DESIGN.md        the one rule, and what is not in v0.1
```

## Roadmap

v0.2 scenario ladder (spot × vol), limit engine + alerts with reasons, execution page via
[tcakit](https://github.com/charlieyanhx/tcakit) · v0.3 strategy-health page, one live
underlying feed, recorded demo · v0.4 textual TUI, Grafana export.

Everything here is synthetic. Live positions and connector configs never enter the repo.

MIT © Hanxiong (Charlie) Yan
