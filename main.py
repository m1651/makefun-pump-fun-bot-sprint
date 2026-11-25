"""Pump.fun floor-sniper bot (agent-fun/replay/v2).

Strategy (my own design, tuned on the kit's frozen dev window offline):

A pump.fun bonding curve cannot trade below its creation price: the price is
virtual_sol_reserves / virtual_token_reserves, and the virtual SOL reserve
starts at 30 SOL and only falls back toward 30 as net buying falls, while the
virtual token reserve only rises back toward its starting value. So a coin
bought near its creation price has a hard floor under it, and the upside is
the fat tail: a coin that completes its curve pays exactly 14.7x its creation
price. Chasing spikes does not work here: the replay fills orders 2 slots
after they are decided, and a spike is usually over by then, so momentum
entries buy the top and mean-revert to the floor.

This bot therefore buys early and near the floor, and lets winners run:

* Entry: a coin whose very first trade is a small organic buy (under 0.75
  SOL -- big first trades are whales who dump afterwards). As soon as the
  1%-of-real-reserves size cap allows a buy of at least 0.01 SOL, and the
  price is still within 10% of its creation price and has never spiked above
  15% over it, buy min(0.03 SOL, the cap). One entry per coin, ever.
* Winners: once a coin trades at 1.30x its creation price or more, never
  sell it -- hold it for completion (14.7x) or the end-of-window flatten.
* Losers: a coin sitting within 8% of its creation-price floor that has had
  no trade for 500 slots, or is 200 slots old, is dead money; sell it in
  full to free one of the 5 position slots for a fresh coin.

All decisions use integer cross-multiplication on the reserves, so there is
no floating point, no clock, no environment read and no randomness anywhere:
two executions produce byte-identical action logs.

The replay shim lives in vendor/makefun_bot.py (the kit's own SDK file, not
my code) and is put on sys.path here because the grader runs this file with
``python3 -P -s``. stdout carries replies only; diagnostics go to stderr.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script directory off sys.path)

LAMPORTS_PER_SOL = makefun_bot.LAMPORTS_PER_SOL

# --- tunables (lamports unless noted) -------------------------------------
STAKE_LAMPORTS = 30_000_000          # 0.03 SOL target stake per coin
MIN_BUY_LAMPORTS = 10_000_000       # 0.01 SOL: smallest buy worth the fees,
                                    # and the round-trip bar per coin
FIRST_TRADE_MAX_LAMPORTS = 750_000_000  # 0.75 SOL: first trade must be small
MAX_SLIPPAGE_BPS = 10_000           # wide open: never fail on slippage

# Entry: price within 10% of creation price (10/11), peak never above 15%
# (20/23). Winner: price at/above 1.30x creation price (10/13) is held.
# Dead: price within 8% of creation price (25/27) and stale or old.
ENTRY_PRICE_NUM, ENTRY_PRICE_DEN = 11, 10
ENTRY_PEAK_NUM, ENTRY_PEAK_DEN = 23, 20
WINNER_NUM, WINNER_DEN = 13, 10
DEAD_PRICE_NUM, DEAD_PRICE_DEN = 27, 25
DEAD_STALE_SLOTS = 500
DEAD_MIN_AGE_SLOTS = 200


def _validate_tunables() -> None:
    """Fail fast at import if the ratio tunables contradict each other.

    The strategy only makes sense when the bands are ordered: the dead
    (sell) price must sit below the entry price, the entry price below
    the peak ceiling, and the winner threshold above the peak ceiling.
    A typo in any numerator/denominator would otherwise silently turn
    entries into instant sells (or winners into never-sells) for the
    whole run, with no error anywhere. This runs once at import, adds
    no per-batch cost, and changes no trading decision.
    """
    entry = ENTRY_PRICE_NUM / ENTRY_PRICE_DEN
    peak = ENTRY_PEAK_NUM / ENTRY_PEAK_DEN
    winner = WINNER_NUM / WINNER_DEN
    dead = DEAD_PRICE_NUM / DEAD_PRICE_DEN
    if not (1.0 < dead < entry < peak < winner):
        raise SystemExit(
            f"inconsistent tunables: dead {dead:.3f}, entry {entry:.3f}, "
            f"peak {peak:.3f}, winner {winner:.3f} must satisfy "
            "1 < dead < entry < peak < winner"
        )
    if STAKE_LAMPORTS < MIN_BUY_LAMPORTS or MIN_BUY_LAMPORTS <= 0:
        raise SystemExit("inconsistent tunables: 0 < MIN_BUY <= STAKE required")
    if DEAD_STALE_SLOTS <= 0 or DEAD_MIN_AGE_SLOTS <= 0:
        raise SystemExit("inconsistent tunables: dead slots must be positive")


_validate_tunables()


def _leq_ratio(vq, vt, vq0, vt0, num, den):
    """True when vq/vt <= (num/den) * (vq0/vt0), in exact integer math."""
    return den * vq * vt0 <= num * vq0 * vt


def _geq_ratio(vq, vt, vq0, vt0, num, den):
    """True when vq/vt >= (num/den) * (vq0/vt0), in exact integer math."""
    return den * vq * vt0 >= num * vq0 * vt


class Coin:
    """Per-mint state tracked from the events this bot has seen."""

    __slots__ = (
        "mint", "vq0", "vt0", "create_slot", "vq", "vt", "rq",
        "peak_vq", "peak_vt", "last_slot", "first_sol", "first_buy",
        "complete", "trades",
    )

    def __init__(self, mint):
        self.mint = mint
        self.vq0 = 0
        self.vt0 = 0
        self.create_slot = None
        self.vq = 0
        self.vt = 0
        self.rq = 0
        self.peak_vq = 0
        self.peak_vt = 0
        self.last_slot = -1
        self.first_sol = None
        self.first_buy = None
        self.complete = False
        self.trades = 0

    def price(self):
        return (self.vq, self.vt)

    def note_price(self):
        """Fold the current reserves into the running peak price."""
        if self.vt == 0:
            return
        if self.peak_vt == 0 or self.vq * self.peak_vt > self.peak_vq * self.vt:
            self.peak_vq = self.vq
            self.peak_vt = self.vt


# Coins seen, in first-seen order (dicts keep insertion order: deterministic).
coins = {}
# Mints already bought once: never re-entered, win or lose.
entered = set()


def _coin(mint):
    coin = coins.get(mint)
    if coin is None:
        coin = coins[mint] = Coin(mint)
    return coin


def on_batch(state, batch):
    slot = batch["slot"]
    # 1. Fold this batch's events into per-coin state, in delivered order.
    for event in batch["events"]:
        mint = event["mint"]
        coin = _coin(mint)
        coin.last_slot = slot
        kind = event["event"]
        if kind == "CreateEvent":
            coin.vq0 = int(event["virtual_sol_reserves"])
            coin.vt0 = int(event["virtual_token_reserves"])
            coin.vq = coin.vq0
            coin.vt = coin.vt0
            coin.rq = 0
            coin.create_slot = event["slot"]
            coin.peak_vq = coin.vq0
            coin.peak_vt = coin.vt0
        elif kind == "TradeEvent":
            coin.vq = int(event["virtual_sol_reserves"])
            coin.vt = int(event["virtual_token_reserves"])
            coin.rq = int(event["real_sol_reserves"])
            coin.trades += 1
            if coin.first_sol is None:
                coin.first_sol = int(event["sol_amount"])
                coin.first_buy = bool(event["is_buy"])
            if coin.vq0 == 0:
                # No CreateEvent seen first: use the first trade's reserves
                # as the creation price rather than discarding the coin.
                coin.vq0 = coin.vq
                coin.vt0 = coin.vt
                coin.peak_vq = coin.vq
                coin.peak_vt = coin.vt
            coin.note_price()
        else:  # CompleteEvent
            coin.complete = True

    caps = state.hello["caps"]
    tx = state.tx_fee_lamports()
    free = state.free_cash_lamports()
    orders = []

    # 2. Exits first: free slots before deciding entries.
    pending_sell = set()
    for pending in state.pending.values():
        if pending.order["side"] == "sell":
            pending_sell.add(pending.order["mint"])
    for mint, holding in list(state.positions.items()):
        if mint in pending_sell or holding.token_amount <= 0:
            continue
        coin = coins.get(mint)
        if coin is None or coin.vt == 0 or coin.complete:
            continue
        vq, vt = coin.vq, coin.vt
        vq0, vt0 = coin.vq0, coin.vt0
        if vq0 == 0 or vt0 == 0:
            continue
        if _geq_ratio(vq, vt, vq0, vt0, WINNER_NUM, WINNER_DEN):
            # Winner: hold for completion / the end-of-window flatten.
            continue
        age = None if coin.create_slot is None else slot - coin.create_slot
        stale = slot - coin.last_slot >= DEAD_STALE_SLOTS
        old = age is not None and age >= DEAD_MIN_AGE_SLOTS
        if _leq_ratio(vq, vt, vq0, vt0, DEAD_PRICE_NUM, DEAD_PRICE_DEN) and (stale or old):
            if free < tx:
                continue
            orders.append(
                {
                    "side": "sell",
                    "mint": mint,
                    "token_amount": holding.token_amount,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }
            )
            free -= tx
            pending_sell.add(mint)

    # 3. Entries: one per coin, only on its own TradeEvents in this batch.
    pending_buy = set()
    for pending in state.pending.values():
        if pending.order["side"] == "buy":
            pending_buy.add(pending.order["mint"])
    committed = state.committed_position_count()
    for event in batch["events"]:
        if event["event"] != "TradeEvent":
            continue
        mint = event["mint"]
        if mint in entered or mint in state.positions or mint in pending_buy:
            continue
        if committed >= caps["max_open_positions"]:
            break
        coin = coins.get(mint)
        if coin is None or coin.complete:
            continue
        if coin.vq0 == 0 or coin.vt0 == 0 or coin.vt == 0:
            continue
        # Organic start only: a small first buy, never a whale's first trade.
        if coin.first_sol is None or not coin.first_buy:
            continue
        if coin.first_sol > FIRST_TRADE_MAX_LAMPORTS:
            continue
        # Still near the floor, and never spiked: buy before any run-up.
        if not _leq_ratio(coin.vq, coin.vt, coin.vq0, coin.vt0,
                          ENTRY_PRICE_NUM, ENTRY_PRICE_DEN):
            continue
        if not _leq_ratio(coin.peak_vq, coin.peak_vt, coin.vq0, coin.vt0,
                          ENTRY_PEAK_NUM, ENTRY_PEAK_DEN):
            continue
        cap = state.max_buy_lamports(mint)
        if cap < MIN_BUY_LAMPORTS:
            continue
        amount = cap if cap < STAKE_LAMPORTS else STAKE_LAMPORTS
        if free < amount + tx:
            continue
        if len(orders) >= caps["max_orders_per_batch"]:
            break
        orders.append(
            {
                "side": "buy",
                "mint": mint,
                "quote_lamports": amount,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }
        )
        entered.add(mint)
        pending_buy.add(mint)
        committed += 1
        free -= amount + tx
    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch)
