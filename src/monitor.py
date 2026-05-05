"""
WebSocket data stream orchestrator for market making.

Runs concurrent order book streams for all configured symbols,
maintains account balances, and updates limit orders via the strategy engine.
"""

import asyncio
import time
from typing import Dict

from src.config import BotConfig
from src.exchange import ExchangeClient
from src.strategy import StrategyEngine
from src.executor import TradeExecutor
from src.utils.logger import setup_logger, log_with_data


class Monitor:
    """
    Orchestrates WebSocket streams and the market making event loop.
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
        
        self.balances: Dict[str, float] = {}
        
        if self.config.dry_run:
            # Initialize paper trading balances with the configured amount
            self.balances["USD"] = self.config.dry_run_balance_usd
            # Starting with 0 base assets allows watching the initial accumulation phase
            for symbol in self.config.symbols:
                base = symbol.split("/")[0]
                self.balances[base] = 0.0

    async def start(self) -> None:
        """Start monitoring configured symbols."""
        self._running = True
        self._last_heartbeat = time.time()

        if not self.config.symbols:
            self.logger.error("No symbols configured. Exiting.")
            return

        for symbol in self.config.symbols:
            log_with_data(self.logger, "info", "Monitoring symbol", symbol=symbol)

        # Give WebSocket a moment to receive initial snapshots
        self.logger.info("Waiting for initial WebSocket snapshots...")
        await asyncio.sleep(3)

        # Set fill callback for paper trading updates
        self.executor.on_fill = self._handle_fill

        tasks = []
        for symbol in self.config.symbols:
            tasks.append(self._watch_symbol(symbol))

        tasks.append(self._balance_loop())
        tasks.append(self._heartbeat_loop())
        tasks.append(self._sync_orders_loop())

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

    async def _balance_loop(self) -> None:
        """Periodically fetch balance to update inventory for strategy engine."""
        if self.config.dry_run:
            return

        # Initial fetch
        try:
            balance_data = await self.exchange.fetch_balance()
            for asset, data in balance_data.items():
                if isinstance(data, dict) and "free" in data:
                    self.balances[asset] = data["free"]
        except Exception as e:
            self.logger.error(f"Initial balance fetch error: {e}")

        while self._running:
            try:
                await asyncio.sleep(5)  # Fetch balance every 5 seconds
                balance_data = await self.exchange.fetch_balance()
                for asset, data in balance_data.items():
                    if isinstance(data, dict) and "free" in data:
                        self.balances[asset] = data["free"]
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Balance fetch error: {e}")

    async def _sync_orders_loop(self) -> None:
        """Periodically sync open orders to detect fills and update executor state."""
        while self._running:
            try:
                await asyncio.sleep(2)  # Sync every 2 seconds
                for symbol in self.config.symbols:
                    await self.executor.sync_open_orders(symbol)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Sync orders error: {e}")

    async def _watch_symbol(self, symbol: str) -> None:
        """
        Continuously watch the order book for a symbol.
        On each update, evaluate optimal market making quotes and update orders.
        """
        reconnect_delay = 1
        base_asset = symbol.split("/")[0]
        quote_asset = symbol.split("/")[1]

        while self._running:
            try:
                orderbook = await self.exchange.wait_orderbook_update(symbol)
                self.strategy.update_book(symbol, orderbook)
                self._tick_count += 1

                # Need balances to evaluate inventory skew
                base_balance = self.balances.get(base_asset, 0.0)
                quote_balance = self.balances.get(quote_asset, 0.0)

                signal = self.strategy.evaluate(symbol, base_balance, quote_balance)
                if signal:
                    await self.executor.update_orders(signal)

                reconnect_delay = 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_with_data(self.logger, "warning", f"Stream error for {symbol}", error=str(e))
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

    async def _heartbeat_loop(self) -> None:
        """Log periodic heartbeat with status summary."""
        while self._running:
            try:
                await asyncio.sleep(self.config.heartbeat_interval_sec)
                now = time.time()
                elapsed = now - self._last_heartbeat

                status = []
                for symbol in self.config.symbols:
                    book = self.strategy.books.get(symbol)
                    mid = book.mid_price if book else 0.0
                    
                    base_asset = symbol.split("/")[0]
                    quote_asset = symbol.split("/")[1]
                    base_balance = self.balances.get(base_asset, 0.0)
                    quote_balance = self.balances.get(quote_asset, 0.0)
                    
                    status.append({
                        "symbol": symbol,
                        "mid": round(mid, 4),
                        "base_bal": round(base_balance, 4),
                        "quote_bal": round(quote_balance, 2),
                    })

                log_with_data(
                    self.logger, "info",
                    "💓 Heartbeat",
                    ticks=self._tick_count,
                    uptime_sec=round(elapsed, 0),
                    status=status
                )

                self._tick_count = 0
                self._last_heartbeat = now
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Heartbeat error: {e}")

    def _handle_fill(self, symbol: str, side: str, price: float, amount: float) -> None:
        """Update local simulated balances when a paper trade fills."""
        if not self.config.dry_run:
            return

        base_asset = symbol.split("/")[0]
        quote_asset = symbol.split("/")[1]
        
        cost = price * amount
        
        if side == "buy":
            self.balances[base_asset] = self.balances.get(base_asset, 0.0) + amount
            self.balances[quote_asset] = self.balances.get(quote_asset, 0.0) - cost
        else:
            self.balances[base_asset] = self.balances.get(base_asset, 0.0) - amount
            self.balances[quote_asset] = self.balances.get(quote_asset, 0.0) + cost

        log_with_data(
            self.logger, "info", "Paper Balance Updated",
            asset=base_asset, balance=round(self.balances[base_asset], 4),
            quote=quote_asset, quote_balance=round(self.balances[quote_asset], 2)
        )
