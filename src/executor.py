"""
Trade executor for the market making bot.

Manages the lifecycle of limit orders (quoting, cancelling, replacing)
based on signals from the strategy engine.
"""

import asyncio
import csv
import os
import time
from typing import Dict, Optional, Tuple

from src.config import BotConfig
from src.exchange import ExchangeClient
from src.strategy import MarketMakerSignal, StrategyEngine
from src.utils.logger import setup_logger, log_with_data


class ActiveMarketMake:
    """Tracks active limit orders for a symbol."""
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.buy_order_id: Optional[str] = None
        self.sell_order_id: Optional[str] = None
        self.target_buy_price: float = 0.0
        self.target_sell_price: float = 0.0


class TradeExecutor:
    """
    Executes and manages limit orders for market making.
    """

    def __init__(
        self,
        config: BotConfig,
        exchange: ExchangeClient,
        strategy: StrategyEngine,
    ):
        self.config = config
        self.exchange = exchange
        self.strategy = strategy
        self.logger = setup_logger("zephyr.executor", config.log_level)
        
        # Callback to notify when an order fills
        self.on_fill = None
        
        self.active_orders: Dict[str, ActiveMarketMake] = {
            symbol: ActiveMarketMake(symbol) for symbol in config.all_symbols
        }
        
        # Prevent concurrent executions per symbol
        self._executing: Dict[str, bool] = {
            symbol: False for symbol in config.all_symbols
        }

    async def update_orders(self, signal: MarketMakerSignal) -> None:
        """
        Update quotes for a symbol.
        
        1. Checks if current active orders are too far from the optimal prices.
        2. Cancels stale orders.
        3. Places new limit orders at the optimal prices.
        """
        if self._executing[signal.symbol]:
            return
            
        self._executing[signal.symbol] = True
        try:
            active = self.active_orders[signal.symbol]

            def needs_replace(current_target: float, new_target: float) -> bool:
                if current_target <= 0:
                    return True
                deviation = abs(new_target - current_target) / current_target * 100
                return deviation > self.config.order_refresh_tolerance_pct

            replace_buy = not active.buy_order_id or needs_replace(active.target_buy_price, signal.buy_price)
            replace_sell = not active.sell_order_id or needs_replace(active.target_sell_price, signal.sell_price)

            if not replace_buy and not replace_sell:
                # Prices are still within tolerance, do nothing
                return

            log_with_data(
                self.logger, "info",
                "Updating quotes",
                symbol=signal.symbol,
                mid_price=signal.mid_price,
                buy_price=signal.buy_price,
                sell_price=signal.sell_price,
                inventory_delta=round(signal.inventory_delta, 4),
            )

            # Calculate trade amount in base units
            amount = self.config.trade_amount_usd / signal.mid_price

            if self.config.dry_run:
                active.target_buy_price = signal.buy_price
                active.target_sell_price = signal.sell_price
                active.buy_order_id = "dry_run_buy"
                active.sell_order_id = "dry_run_sell"
                return

            # Cancel old orders if we are replacing them
            cancel_tasks = []
            if replace_buy and active.buy_order_id:
                cancel_tasks.append(self.exchange.cancel_order(active.buy_order_id, signal.symbol))
                active.buy_order_id = None
            
            if replace_sell and active.sell_order_id:
                cancel_tasks.append(self.exchange.cancel_order(active.sell_order_id, signal.symbol))
                active.sell_order_id = None
                
            if cancel_tasks:
                await asyncio.gather(*cancel_tasks, return_exceptions=True)

            # Place new limit orders
            if replace_buy:
                buy_order = await self.exchange.create_limit_buy(signal.symbol, amount, signal.buy_price)
                if buy_order and not isinstance(buy_order, Exception) and buy_order.get("id"):
                    active.buy_order_id = buy_order.get("id")
                    active.target_buy_price = signal.buy_price

            if replace_sell:
                sell_order = await self.exchange.create_limit_sell(signal.symbol, amount, signal.sell_price)
                if sell_order and not isinstance(sell_order, Exception) and sell_order.get("id"):
                    active.sell_order_id = sell_order.get("id")
                    active.target_sell_price = signal.sell_price

        except Exception as e:
            log_with_data(
                self.logger, "error",
                f"Failed to update orders: {e}",
                symbol=signal.symbol,
            )
        finally:
            self._executing[signal.symbol] = False

    async def sync_open_orders(self, symbol: str) -> None:
        """
        Fetch open orders from the exchange to reconcile local state.
        If an order we track is no longer open, it was likely filled.
        """
        active = self.active_orders[symbol]

        if self.config.dry_run:
            # Simulate fills based on current orderbook
            book = self.strategy.books.get(symbol)
            if not book:
                return
                
            amount = self.config.trade_amount_usd / book.mid_price
            
            if active.buy_order_id and book.best_ask <= active.target_buy_price:
                log_with_data(self.logger, "info", "DRY-RUN: Buy order filled", price=active.target_buy_price, symbol=symbol)
                self._log_trade_to_csv(symbol, "buy", active.target_buy_price, amount)
                if self.on_fill:
                    self.on_fill(symbol, "buy", active.target_buy_price, amount)
                active.buy_order_id = None
                
            if active.sell_order_id and book.best_bid >= active.target_sell_price:
                log_with_data(self.logger, "info", "DRY-RUN: Sell order filled", price=active.target_sell_price, symbol=symbol)
                self._log_trade_to_csv(symbol, "sell", active.target_sell_price, amount)
                if self.on_fill:
                    self.on_fill(symbol, "sell", active.target_sell_price, amount)
                active.sell_order_id = None
            return
            
        try:
            open_orders = await self.exchange.fetch_open_orders(symbol)
            open_ids = {o.get("id") for o in open_orders if o.get("id")}
            
            if active.buy_order_id and active.buy_order_id not in open_ids:
                amount = self.config.trade_amount_usd / active.target_buy_price
                log_with_data(self.logger, "info", "Buy order filled", order_id=active.buy_order_id, symbol=symbol)
                self._log_trade_to_csv(symbol, "buy", active.target_buy_price, amount)
                if self.on_fill:
                    self.on_fill(symbol, "buy", active.target_buy_price, amount)
                active.buy_order_id = None
            
            if active.sell_order_id and active.sell_order_id not in open_ids:
                amount = self.config.trade_amount_usd / active.target_sell_price
                log_with_data(self.logger, "info", "Sell order filled", order_id=active.sell_order_id, symbol=symbol)
                self._log_trade_to_csv(symbol, "sell", active.target_sell_price, amount)
                if self.on_fill:
                    self.on_fill(symbol, "sell", active.target_sell_price, amount)
                active.sell_order_id = None
                
        except Exception as e:
            self.logger.error(f"Failed to sync open orders for {symbol}: {e}")

    def _log_trade_to_csv(self, symbol: str, side: str, price: float, amount: float) -> None:
        """Append the completed trade to a CSV ledger."""
        os.makedirs("data", exist_ok=True)
        csv_path = "data/trades.csv"
        file_exists = os.path.isfile(csv_path)

        try:
            with open(csv_path, mode="a", newline="") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow([
                        "time", "symbol", "side", "price", "amount", "notional_usd"
                    ])

                writer.writerow([
                    time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                    symbol,
                    side,
                    price,
                    round(amount, 8),
                    round(price * amount, 2),
                ])
        except Exception as e:
            self.logger.error(f"Failed to write to CSV: {e}")
