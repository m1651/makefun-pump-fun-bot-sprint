# Pump.fun Bot Sprint — Floor-Sniper Bot

[![make.fun · u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![make.fun · u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/m1651/makefun-pump-fun-bot-sprint/rank.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![make.fun · u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/m1651/makefun-pump-fun-bot-sprint/reward.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)

A Python trading bot for the [Pump.fun Bot Sprint: Make Bot And Win](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed) bounty. It trades paper SOL on a replay of pump.fun bonding-curve chain data under the `agent-fun/replay/v2` protocol, with 10 SOL of capital, at most 5 open positions, and no shorts or leverage.

## Strategy: buy near the floor, let winners run

A pump.fun bonding curve cannot trade below its creation price: the price is
`virtual_sol_reserves / virtual_token_reserves`, the virtual SOL reserve
starts at 30 SOL and only falls back toward 30 as net buying falls, while the
virtual token reserve only rises back toward its starting value. A coin bought
near its creation price therefore has a hard floor under it, and the upside is
a fat tail — a coin that completes its curve pays exactly 14.7x its creation
price. Chasing spikes does not work here: the replay fills orders 2 slots
after they are decided, and a spike is usually over by then, so momentum
entries buy the top and mean-revert to the floor (verified offline on the
dev window: momentum entries went from +13% per trade with instant fills to
about −3.5% with the real 2-slot latency).

So this bot buys early and near the floor, and never sells a winner:

- **Entry.** A coin whose very first trade is a small organic buy (under
  0.75 SOL — big first trades are whales who dump afterwards). As soon as the
  1%-of-real-reserves size cap allows a buy of at least 0.01 SOL, and the
  price is still within 10% of its creation price and has never spiked above
  15% over it, buy `min(0.03 SOL, cap)`. One entry per coin, ever.
- **Winners.** Once a coin trades at 1.30x its creation price or more, it is
  never sold — it is held for completion (14.7x) or the end-of-window
  flatten.
- **Losers.** A coin sitting within 8% of its creation-price floor that has
  had no trade for 500 slots, or is 200 slots old, is dead money; it is sold
  in full to free one of the 5 position slots for a fresh coin.

All decisions use exact integer cross-multiplication on the reserves — no
floating point, no clock, no environment reads, no randomness — so two
executions produce byte-identical action logs. `vendor/makefun_bot.py` is the
kit's own Python SDK shim, not my code; the strategy in `main.py` is original.

## How to run

```sh
PYTHONHASHSEED=0 python3 -P -s main.py
```

The bot speaks `agent-fun/replay/v2` on stdin/stdout (hello, batch, done in;
one reply per batch out, echoing the batch's `seq`). Diagnostics go to
stderr; stdout carries replies only.

Grade locally with the kit (process driver where Docker is unavailable):

```sh
node cli/makefun.mjs grade-local --kit <kit-dir> --bot <this repo> --driver process
```

## Local grade result

Offline simulation against the kit's frozen dev window (faithful replay of
the harness rules: 2-slot latency, 1% size cap, 125 bps curve fees,
105,000-lamport tx fees, 5 positions) gave about **−0.03 SOL net PnL**
with **25 coins** completing round trips of at least 0.01 SOL and **max
drawdown ≈ 0.3%** for this parameter set. The official `grade-local` run
is still pending: the build was paused by an execution-risk check before
grading, and the numbers above will be replaced with the graded result once
that run is approved and completed.
