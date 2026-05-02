"""
WebSocket data stream orchestrator.

Runs concurrent order book streams for all monitored symbols,
feeds updates into the strategy engine, and triggers the executor
when profitable spread opportunities are detected.

Uses Kraken's native WebSocket v2 API (free, open-source) via
the raw `websockets` library.
"""

import asyncio
import time
from typing import Dict, List, Optional

from src.config import BotConfig
from src.exchange import ExchangeClient
from src.strategy import StrategyEngine
from src.executor import TradeExecutor
from src.utils.logger import setup_logger, log_with_data


class Monitor:
    """
    Orchestrates WebSocket streams and the arbitrage event loop.

    Subscribes to Kraken WebSocket order book streams for each
    symbol, then runs async loops that await updates and evaluate
    the strategy.
    """

    def __init__(
        self,
        config: BotConfig,
        exchange: ExchangeClient,
        strategy: StrategyEngine,
        executor: TradeExecutor,
    ):
        self.config = config
        self.exchange = exchange
        self.strategy = strategy
        self.executor = executor
        self.logger = setup_logger("zephyr.monitor", config.log_level)

        self._running = False
        self._last_heartbeat = 0.0
        self._tick_count = 0

    async def start(self) -> None:
        """
        Start monitoring all configured spread pairs.
        Launches per-symbol WebSocket consumers and evaluation loops.
        """
        self._running = True
        self._last_heartbeat = time.time()

        if not self.config.spread_pairs:
            self.logger.error("No spread pairs configured. Exiting.")
            return

        # Log the pairs we're monitoring
        for symbol_a, symbol_b in self.config.spread_pairs:
            log_with_data(
                self.logger, "info",
                "Monitoring spread pair",
                symbol_a=symbol_a,
                symbol_b=symbol_b,
            )

        # Give WebSocket a moment to receive initial snapshots
        self.logger.info("Waiting for initial WebSocket snapshots...")
        await asyncio.sleep(3)

        # Launch concurrent async loops — one per symbol
        tasks = []
        all_symbols = self.config.all_symbols
        for symbol in all_symbols:
            tasks.append(self._watch_symbol(symbol))

        # Add heartbeat task
        tasks.append(self._heartbeat_loop())

        # Add exit signal checker
        tasks.append(self._exit_check_loop())

        self.logger.info(
            f"Starting {len(tasks)} async tasks for "
            f"{len(self.config.spread_pairs)} spread pairs"
        )

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            self.logger.info("Monitor tasks cancelled")
        except Exception as e:
            self.logger.error(f"Monitor error: {e}")
        finally:
            self._running = False

    async def stop(self) -> None:
        """Signal the monitor to stop."""
        self._running = False

    async def _watch_symbol(self, symbol: str) -> None:
        """
        Continuously watch the order book for a symbol.
        On each update, feed into strategy and evaluate all
        spread pairs involving this symbol.
        """
        reconnect_delay = 1
        while self._running:
            try:
                orderbook = await self.exchange.wait_orderbook_update(symbol)
                self.strategy.update_book(symbol, orderbook)
                self._tick_count += 1

                # Evaluate all spread pairs involving this symbol
                await self._evaluate_pairs(symbol)

                reconnect_delay = 1

            except asyncio.CancelledError:
                break
            except Exception as e:
                log_with_data(
                    self.logger, "warning",
                    f"Stream error for {symbol}, retrying in {reconnect_delay}s",
                    symbol=symbol,
                    error=str(e),
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

    async def _evaluate_pairs(self, symbol: str) -> None:
        """
        Evaluate all spread pairs involving the given symbol.
        """
        for symbol_a, symbol_b in self.config.spread_pairs:
            if symbol != symbol_a and symbol != symbol_b:
                continue

            signal = self.strategy.evaluate(symbol_a, symbol_b)

            if not signal:
                continue

            # Log spread at DEBUG level on every tick
            log_with_data(
                self.logger, "debug",
                "Spread tick",
                symbol_a=signal.symbol_a,
                symbol_b=signal.symbol_b,
                buy_symbol=signal.buy_symbol,
                sell_symbol=signal.sell_symbol,
                buy_ask=signal.buy_ask,
                sell_bid=signal.sell_bid,
                spread_pct=round(signal.spread_pct, 4),
                net_pct=round(signal.net_of_fees_pct, 4),
                is_entry=signal.is_entry,
            )

            # Execute entry if signal is triggered
            if signal.is_entry:
                log_with_data(
                    self.logger, "info",
                    "🚀 ENTRY SIGNAL TRIGGERED",
                    buy_symbol=signal.buy_symbol,
                    sell_symbol=signal.sell_symbol,
                    net_spread_pct=round(signal.net_of_fees_pct, 4),
                )
                await self.executor.execute_entry(signal)

    async def _exit_check_loop(self) -> None:
        """
        Periodically check for exit signals on open positions.
        Runs every 10 seconds to avoid excessive computation.
        """
        while self._running:
            try:
                exits = self.strategy.check_exit_signals()
                for position in exits:
                    log_with_data(
                        self.logger, "info",
                        "🔻 EXIT SIGNAL — closing position",
                        buy_symbol=position.buy_symbol,
                        sell_symbol=position.sell_symbol,
                    )
                    await self.executor.execute_exit(position)

                await asyncio.sleep(10)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Exit check error: {e}")
                await asyncio.sleep(10)

    async def _heartbeat_loop(self) -> None:
        """
        Log periodic heartbeat with status summary.
        """
        while self._running:
            try:
                await asyncio.sleep(self.config.heartbeat_interval_sec)

                now = time.time()
                elapsed = now - self._last_heartbeat

                # Gather current spread for all pairs
                pair_status = []
                for symbol_a, symbol_b in self.config.spread_pairs:
                    signal = self.strategy.evaluate(symbol_a, symbol_b)
                    if signal:
                        pair_status.append({
                            "pair": f"{symbol_a} ↔ {symbol_b}",
                            "spread_pct": round(signal.spread_pct, 4),
                            "net_pct": round(signal.net_of_fees_pct, 4),
                            "direction": f"buy {signal.buy_symbol}",
                        })

                log_with_data(
                    self.logger, "info",
                    "💓 Heartbeat",
                    ticks_since_last=self._tick_count,
                    open_positions=len(self.strategy.positions),
                    pairs=pair_status,
                    uptime_sec=round(elapsed, 0),
                )

                self._tick_count = 0
                self._last_heartbeat = now

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Heartbeat error: {e}")
