"""
SNRZ Trading Bot — Railway-deployable version.

Connects to your MT5 account through MetaApi.cloud (no local MT5 Terminal
or Windows machine needed), runs the SNRZ support/resistance strategy on
XAUUSD and BTCUSD, and places trades automatically.

Required environment variables (set these in Railway's "Variables" tab):
  METAAPI_TOKEN       - your MetaApi API token
  METAAPI_ACCOUNT_ID  - your MetaApi MetaTrader account id
  SYMBOLS             - comma-separated, e.g. "XAUUSDm,BTCUSDm" (use your
                         broker's exact symbol names, Exness often adds a
                         suffix like "m")
  RISK_PERCENT        - risk per trade, default 1.0
  FIXED_LOT           - fixed lot size, default 0.01
  MAX_POSITIONS       - max open positions per symbol, default 2
  POLL_SECONDS         - how often to check for signals, default 60
  DRY_RUN             - "true" to log signals without placing real orders
                         (STRONGLY recommended while testing)

IMPORTANT: Test with DRY_RUN=true and a demo account first. This is a
straightforward rule-based port of the original MQL5 EA — it has not been
independently verified or extensively backtested. Review the logic in
strategy.py before risking real money.
"""
import asyncio
import os
import logging
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from metaapi_cloud_sdk import MetaApi

from strategy import StrategyConfig, generate_signal, compute_sl_tp

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("snrz-bot")

TOKEN = os.environ["METAAPI_TOKEN"]
ACCOUNT_ID = os.environ["METAAPI_ACCOUNT_ID"]
SYMBOLS = [s.strip() for s in os.environ.get("SYMBOLS", "XAUUSD,BTCUSD").split(",") if s.strip()]
RISK_PERCENT = float(os.environ.get("RISK_PERCENT", "1.0"))
FIXED_LOT = float(os.environ.get("FIXED_LOT", "0.01"))
MAX_POSITIONS = int(os.environ.get("MAX_POSITIONS", "2"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

# Pip sizes differ by instrument — adjust here if your broker's quoting differs.
PIP_SIZE = {
    "XAUUSD": 0.01,   # gold is typically quoted with 2 decimals; 1 pip = 0.01
    "BTCUSD": 1.0,    # crypto CFDs vary widely by broker; verify with your broker's spec
}


def pip_size_for(symbol: str) -> float:
    for key, size in PIP_SIZE.items():
        if symbol.upper().startswith(key):
            return size
    return 0.0001  # fallback for standard forex pairs


async def get_recent_candles(connection, symbol: str, timeframe: str, count: int) -> list[dict]:
    """Fetch recent candles via MetaApi's historical candles endpoint."""
    candles = await connection.get_candles(symbol, timeframe, limit=count)
    # Normalize to oldest -> newest, with the fields our strategy expects.
    candles = sorted(candles, key=lambda c: c["time"])
    return [
        {"open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"]}
        for c in candles
    ]


async def count_open_positions(connection, symbol: str) -> int:
    positions = await connection.get_positions()
    return sum(1 for p in positions if p["symbol"] == symbol)


async def calculate_lot(connection, account_currency_balance: float, symbol: str, sl_pips: float, pip_val_per_lot: float) -> float:
    """
    Risk-based lot sizing. If you'd rather always use FIXED_LOT, this is skipped
    (see main loop below) — kept here in case you want risk-based sizing instead.
    """
    risk_amount = account_currency_balance * (RISK_PERCENT / 100.0)
    if sl_pips <= 0 or pip_val_per_lot <= 0:
        return FIXED_LOT
    lot = risk_amount / (sl_pips * pip_val_per_lot)
    return max(0.01, round(lot, 2))


async def run_symbol(connection, symbol: str, cfg: StrategyConfig):
    try:
        open_count = await count_open_positions(connection, symbol)
        if open_count >= MAX_POSITIONS:
            log.info(f"[{symbol}] max positions ({MAX_POSITIONS}) already open, skipping")
            return

        candles = await get_recent_candles(connection, symbol, "1h", cfg.lookback_candles + 5)
        signal, level = generate_signal(candles, cfg)

        if signal is None:
            log.info(f"[{symbol}] no signal")
            return

        price_info = await connection.get_symbol_price(symbol)
        entry_price = price_info["ask"] if signal == "BUY" else price_info["bid"]
        sl_tp = compute_sl_tp(entry_price, signal, cfg)

        log.info(
            f"[{symbol}] SIGNAL={signal} entry={entry_price:.5f} "
            f"SL={sl_tp['stopLoss']:.5f} TP1={sl_tp['takeProfit1']:.5f} TP2={sl_tp['takeProfit2']:.5f} "
            f"(level touched: {level.price:.5f}, touches={level.touches})"
        )

        if DRY_RUN:
            log.info(f"[{symbol}] DRY_RUN active — not placing a real order")
            return

        if signal == "BUY":
            result = await connection.create_market_buy_order(
                symbol, FIXED_LOT,
                stop_loss=sl_tp["stopLoss"],
                take_profit=sl_tp["takeProfit1"],
            )
        else:
            result = await connection.create_market_sell_order(
                symbol, FIXED_LOT,
                stop_loss=sl_tp["stopLoss"],
                take_profit=sl_tp["takeProfit1"],
            )
        log.info(f"[{symbol}] order result: {result}")

    except Exception as e:
        log.exception(f"[{symbol}] error while processing: {e}")


async def main():
    log.info(f"Starting SNRZ bot | symbols={SYMBOLS} | DRY_RUN={DRY_RUN}")
    api = MetaApi(TOKEN)
    account = await api.metatrader_account_api.get_account(ACCOUNT_ID)

    log.info("Deploying/connecting to MetaTrader account...")
    if account.state != "DEPLOYED":
        await account.deploy()
    await account.wait_connected()

    connection = account.get_rpc_connection()
    await connection.connect()
    await connection.wait_synchronized()
    log.info("Connected and synchronized with MT5 account.")

    cfg_base = StrategyConfig()

    while True:
        for symbol in SYMBOLS:
            cfg = StrategyConfig(
                lookback_candles=cfg_base.lookback_candles,
                min_touches=cfg_base.min_touches,
                touch_tolerance_pips=cfg_base.touch_tolerance_pips,
                stop_loss_pips=cfg_base.stop_loss_pips,
                take_profit_1_pips=cfg_base.take_profit_1_pips,
                take_profit_2_pips=cfg_base.take_profit_2_pips,
                pip_size=pip_size_for(symbol),
            )
            await run_symbol(connection, symbol, cfg)
        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())