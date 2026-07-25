"""
SNRZ Strategy (Support & Resistance Zindan) — ported from MQL5 to Python.

Logic mirrors the original SNRZ_EA.mq5:
  - Scan the last N candles to find Support/Resistance levels.
  - A level is valid once it has been touched at least twice (PO2 = Power of 2nd Touch).
  - BUY signal (RBS - Resistance Breakout to Support): price breaks above
    resistance, then pulls back to a valid support level.
  - SELL signal (SBR - Support Breakout to Resistance): price breaks below
    support, then pulls back to a valid resistance level.
"""
from dataclasses import dataclass
from typing import Literal, Optional

Signal = Literal["BUY", "SELL", None]


@dataclass
class Level:
    price: float
    touches: int
    kind: Literal["support", "resistance"]


@dataclass
class StrategyConfig:
    lookback_candles: int = 20
    min_touches: int = 2          # PO2 - Power of 2nd Touch
    touch_tolerance_pips: float = 5.0
    stop_loss_pips: float = 30.0
    take_profit_1_pips: float = 50.0
    take_profit_2_pips: float = 100.0
    pip_size: float = 0.0001      # overridden per-symbol (gold/BTC use different pip sizes)


def _pips(price_diff: float, pip_size: float) -> float:
    return abs(price_diff) / pip_size


def find_levels(candles: list[dict], tolerance_pips: float, pip_size: float, min_touches: int) -> list[Level]:
    """
    candles: list of dicts with keys open, high, low, close (oldest -> newest)
    Groups swing highs/lows into levels and counts touches.
    """
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    levels: list[Level] = []

    def add_touch(price: float, kind: str):
        tol = tolerance_pips * pip_size
        for lvl in levels:
            if lvl.kind == kind and abs(lvl.price - price) <= tol:
                lvl.touches += 1
                lvl.price = (lvl.price + price) / 2  # refine average
                return
        levels.append(Level(price=price, touches=1, kind=kind))

    for h in highs:
        add_touch(h, "resistance")
    for l in lows:
        add_touch(l, "support")

    return [lvl for lvl in levels if lvl.touches >= min_touches]


def generate_signal(candles: list[dict], cfg: StrategyConfig) -> tuple[Signal, Optional[Level]]:
    """
    Returns (signal, level_used) based on the most recent candle vs. detected levels.
    Needs at least cfg.lookback_candles candles, oldest -> newest.
    """
    if len(candles) < cfg.lookback_candles:
        return None, None

    window = candles[-cfg.lookback_candles:]
    levels = find_levels(window, cfg.touch_tolerance_pips, cfg.pip_size, cfg.min_touches)
    if not levels:
        return None, None

    last = window[-1]
    prev = window[-2]
    close = last["close"]

    resistances = sorted([l for l in levels if l.kind == "resistance"], key=lambda l: l.price)
    supports = sorted([l for l in levels if l.kind == "support"], key=lambda l: l.price)

    tol = cfg.touch_tolerance_pips * cfg.pip_size

    # RBS: previous candle closed above a resistance, current candle pulls back near a support above that resistance
    for r in resistances:
        if prev["close"] > r.price and any(prev2 <= r.price for prev2 in [window[-3]["close"]] if len(window) >= 3):
            for s in supports:
                if s.price > r.price and abs(close - s.price) <= tol:
                    return "BUY", s

    # SBR: previous candle closed below a support, current candle pulls back near a resistance below that support
    for s in supports:
        if prev["close"] < s.price and any(prev2 >= s.price for prev2 in [window[-3]["close"]] if len(window) >= 3):
            for r in resistances:
                if r.price < s.price and abs(close - r.price) <= tol:
                    return "SELL", r

    return None, None


def compute_sl_tp(entry_price: float, signal: Signal, cfg: StrategyConfig) -> dict:
    sl_dist = cfg.stop_loss_pips * cfg.pip_size
    tp1_dist = cfg.take_profit_1_pips * cfg.pip_size
    tp2_dist = cfg.take_profit_2_pips * cfg.pip_size

    if signal == "BUY":
        return {
            "stopLoss": entry_price - sl_dist,
            "takeProfit1": entry_price + tp1_dist,
            "takeProfit2": entry_price + tp2_dist,
        }
    elif signal == "SELL":
        return {
            "stopLoss": entry_price + sl_dist,
            "takeProfit1": entry_price - tp1_dist,
            "takeProfit2": entry_price - tp2_dist,
        }
    raise ValueError("signal must be BUY or SELL")