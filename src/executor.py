"""
Trade executor for the arbitrage bot.

Handles simultaneous order placement for both legs of the
cross-pair spread trade, with dry-run support and fill validation.
"""

import asyncio
import csv
import os
import time
from typing import Dict, Optional, Tuple

from src.config import BotConfig
from src.exchange import ExchangeClient
from src.strategy import ArbitragePosition, SpreadSignal, StrategyEngine
from src.utils.logger import setup_logger, log_with_data


class TradeExecutor:
    """
    Executes cross-pair spread arbitrage trades.

    Places simultaneous spot buy + spot sell orders on
    two correlated pairs, validates fills, and records positions.
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
        self._executing = False  # Prevent concurrent executions

    async def execute_entry(self, signal: SpreadSignal) -> Optional[ArbitragePosition]:
        """
        Execute a cross-pair spread entry trade.

        1. Calculate position size based on trade_amount_usd
        2. Simultaneously place buy order on cheap side + sell order on expensive side
        3. Validate fills and record the position

        Args:
            signal: The entry signal with current prices

        Returns:
            ArbitragePosition if successful, None if failed or dry-run
        """
        if self._executing:
            self.logger.warning("Skipping entry: another execution is in progress")
            return None

        self._executing = True
        try:
            # Calculate position size in base asset units
            amount = self.config.trade_amount_usd / signal.buy_ask

            log_with_data(
                self.logger, "info",
                "Entry signal detected",
                buy_symbol=signal.buy_symbol,
                sell_symbol=signal.sell_symbol,
                buy_ask=signal.buy_ask,
                sell_bid=signal.sell_bid,
                spread_pct=round(signal.spread_pct, 4),
                net_of_fees_pct=round(signal.net_of_fees_pct, 2),
                amount=round(amount, 8),
                notional_usd=self.config.trade_amount_usd,
            )

            if self.config.dry_run:
                # Create a theoretical position for tracking in dry-run
                actual_spread_pct = ((signal.sell_bid - signal.buy_ask) / signal.buy_ask) * 100
                position = ArbitragePosition(
                    buy_symbol=signal.buy_symbol,
                    sell_symbol=signal.sell_symbol,
                    amount=amount,
                    buy_entry_price=signal.buy_ask,
                    sell_entry_price=signal.sell_bid,
                    entry_spread_pct=actual_spread_pct,
                    entry_time=time.time(),
                )
                self.strategy.add_position(position)

                log_with_data(
                    self.logger, "info",
                    "DRY-RUN: Theoretical entry EXECUTED",
                    buy_symbol=signal.buy_symbol,
                    sell_symbol=signal.sell_symbol,
                    amount=round(amount, 8),
                    notional_usd=self.config.trade_amount_usd,
                )
                return position

            # Check balance before executing
            balance_ok = await self._check_balance(signal, amount)
            if not balance_ok:
                self.logger.warning("Insufficient balance for trade, skipping")
                return None

            # Execute both legs simultaneously
            buy_order, sell_order = await self._place_both_legs(
                signal.buy_symbol,
                signal.sell_symbol,
                amount,
            )

            if not buy_order or not sell_order:
                self.logger.error("One or both legs failed, manual intervention may be needed")
                return None

            # Extract fill prices
            buy_fill = buy_order.get("average") or buy_order.get("price") or signal.buy_ask
            sell_fill = sell_order.get("average") or sell_order.get("price") or signal.sell_bid

            # Validate fills against expected prices
            self._validate_fills(signal, buy_fill, sell_fill)

            # Record the position
            actual_spread_pct = ((sell_fill - buy_fill) / buy_fill) * 100
            position = ArbitragePosition(
                buy_symbol=signal.buy_symbol,
                sell_symbol=signal.sell_symbol,
                amount=amount,
                buy_entry_price=buy_fill,
                sell_entry_price=sell_fill,
                entry_spread_pct=actual_spread_pct,
                entry_time=time.time(),
            )

            self.strategy.add_position(position)

            log_with_data(
                self.logger, "info",
                "Entry trade EXECUTED successfully",
                buy_order_id=buy_order.get("id"),
                sell_order_id=sell_order.get("id"),
                buy_fill_price=buy_fill,
                sell_fill_price=sell_fill,
                actual_spread_pct=round(actual_spread_pct, 4),
                expected_spread_pct=round(signal.spread_pct, 4),
            )

            return position

        except Exception as e:
            log_with_data(
                self.logger, "error",
                f"Entry execution failed: {e}",
                buy_symbol=signal.buy_symbol,
                sell_symbol=signal.sell_symbol,
            )
            return None
        finally:
            self._executing = False

    async def execute_exit(self, position: ArbitragePosition) -> bool:
        """
        Exit an open arbitrage position.

        1. Sell the asset we bought
        2. Buy back the asset we sold
        3. Remove the position from tracking

        Args:
            position: The position to close

        Returns:
            True if exit was successful
        """
        if self._executing:
            self.logger.warning("Skipping exit: another execution is in progress")
            return False

        self._executing = True
        try:
            log_with_data(
                self.logger, "info",
                "Executing exit trade",
                buy_symbol=position.buy_symbol,
                sell_symbol=position.sell_symbol,
                amount=round(position.amount, 8),
            )

            if self.config.dry_run:
                # Estimate exit prices from order book for dry-run logging
                book_buy = self.strategy.books.get(position.buy_symbol)
                book_sell = self.strategy.books.get(position.sell_symbol)
                
                sell_exit_price = book_buy.best_bid if book_buy else position.buy_entry_price
                buy_exit_price = book_sell.best_ask if book_sell else position.sell_entry_price
                
                buy_side_pnl = (sell_exit_price - position.buy_entry_price) * position.amount
                sell_side_pnl = (position.sell_entry_price - buy_exit_price) * position.amount
                total_pnl = buy_side_pnl + sell_side_pnl

                log_with_data(
                    self.logger, "info",
                    "DRY-RUN: Theoretical exit EXECUTED",
                    buy_symbol=position.buy_symbol,
                    sell_symbol=position.sell_symbol,
                    total_pnl_usd=round(total_pnl, 4),
                )
                
                self._log_trade_to_csv(
                    position=position,
                    buy_side_pnl=buy_side_pnl,
                    sell_side_pnl=sell_side_pnl,
                    total_pnl=total_pnl
                )
                
                self.strategy.remove_position(position)
                return True

            # Close both legs simultaneously:
            # Sell the asset we bought, buy back the asset we sold
            try:
                sell_close, buy_close = await asyncio.gather(
                    self.exchange.create_market_sell(
                        position.buy_symbol, position.amount
                    ),
                    self.exchange.create_market_buy(
                        position.sell_symbol, position.amount
                    ),
                    return_exceptions=True,
                )
            except Exception as e:
                self.logger.error(f"Exit execution error: {e}")
                return False

            # Log results
            if isinstance(sell_close, Exception):
                self.logger.error(f"Sell close failed: {sell_close}")
                return False
            if isinstance(buy_close, Exception):
                self.logger.error(f"Buy-back close failed: {buy_close}")
                return False

            sell_exit_price = sell_close.get("average") or sell_close.get("price", 0)
            buy_exit_price = buy_close.get("average") or buy_close.get("price", 0)

            # Calculate realized P&L
            # Entry: bought at buy_entry_price, sold at sell_entry_price
            # Exit: sold at sell_exit_price, bought at buy_exit_price
            buy_side_pnl = (sell_exit_price - position.buy_entry_price) * position.amount
            sell_side_pnl = (position.sell_entry_price - buy_exit_price) * position.amount
            total_pnl = buy_side_pnl + sell_side_pnl

            log_with_data(
                self.logger, "info",
                "Exit trade EXECUTED successfully",
                sell_exit_price=sell_exit_price,
                buy_exit_price=buy_exit_price,
                buy_side_pnl_usd=round(buy_side_pnl, 4),
                sell_side_pnl_usd=round(sell_side_pnl, 4),
                total_pnl_usd=round(total_pnl, 4),
                hold_time_sec=round(time.time() - position.entry_time, 0),
            )

            self._log_trade_to_csv(
                position=position,
                buy_side_pnl=buy_side_pnl,
                sell_side_pnl=sell_side_pnl,
                total_pnl=total_pnl
            )

            self.strategy.remove_position(position)
            return True

        except Exception as e:
            log_with_data(
                self.logger, "error",
                f"Exit execution failed: {e}",
                buy_symbol=position.buy_symbol,
                sell_symbol=position.sell_symbol,
            )
            return False
        finally:
            self._executing = False

    async def _place_both_legs(
        self,
        buy_symbol: str,
        sell_symbol: str,
        amount: float,
    ) -> Tuple[Optional[Dict], Optional[Dict]]:
        """
        Place buy and sell orders simultaneously.
        Uses asyncio.gather for near-simultaneous execution.
        """
        try:
            results = await asyncio.gather(
                self.exchange.create_market_buy(buy_symbol, amount),
                self.exchange.create_market_sell(sell_symbol, amount),
                return_exceptions=True,
            )

            buy_order = results[0] if not isinstance(results[0], Exception) else None
            sell_order = results[1] if not isinstance(results[1], Exception) else None

            if isinstance(results[0], Exception):
                self.logger.error(f"Buy order failed: {results[0]}")
            if isinstance(results[1], Exception):
                self.logger.error(f"Sell order failed: {results[1]}")

            return buy_order, sell_order

        except Exception as e:
            self.logger.error(f"Failed to place both legs: {e}")
            return None, None

    async def _check_balance(self, signal: SpreadSignal, amount: float) -> bool:
        """
        Check if we have sufficient balance for the trade.

        For cross-pair arb:
        - Buy side needs quote currency (e.g., USD or USDT)
        - Sell side needs base currency (e.g., BTC) — but we may
          already hold it or be acquiring it from the buy side
        """
        try:
            balance = await self.exchange.fetch_balance()

            # Check buy-side quote currency
            buy_quote = signal.buy_symbol.split("/")[1]  # e.g., "USD"
            buy_quote_free = balance.get(buy_quote, {}).get("free", 0) or 0
            buy_cost = signal.buy_ask * amount

            if buy_quote_free < buy_cost:
                log_with_data(
                    self.logger, "warning",
                    f"Insufficient {buy_quote} balance for buy leg",
                    available=round(buy_quote_free, 2),
                    required=round(buy_cost, 2),
                )
                return False

            # Check sell-side base currency
            sell_base = signal.sell_symbol.split("/")[0]  # e.g., "BTC"
            sell_base_free = balance.get(sell_base, {}).get("free", 0) or 0

            if sell_base_free < amount:
                log_with_data(
                    self.logger, "warning",
                    f"Insufficient {sell_base} balance for sell leg",
                    available=round(sell_base_free, 8),
                    required=round(amount, 8),
                )
                return False

            return True
        except Exception as e:
            self.logger.error(f"Balance check failed: {e}")
            return False

    def _validate_fills(
        self, signal: SpreadSignal, buy_fill: float, sell_fill: float
    ) -> None:
        """Log a warning if fill prices deviate significantly from signal prices."""
        buy_slippage = abs(buy_fill - signal.buy_ask) / signal.buy_ask * 100
        sell_slippage = abs(sell_fill - signal.sell_bid) / signal.sell_bid * 100

        if buy_slippage > 0.1:  # > 0.1% slippage
            log_with_data(
                self.logger, "warning",
                "High buy slippage",
                expected=signal.buy_ask,
                actual=buy_fill,
                slippage_pct=round(buy_slippage, 4),
            )

        if sell_slippage > 0.1:
            log_with_data(
                self.logger, "warning",
                "High sell slippage",
                expected=signal.sell_bid,
                actual=sell_fill,
                slippage_pct=round(sell_slippage, 4),
            )

    def _log_trade_to_csv(
        self,
        position: ArbitragePosition,
        buy_side_pnl: float,
        sell_side_pnl: float,
        total_pnl: float,
    ) -> None:
        """Append the completed trade to a CSV ledger."""
        os.makedirs("data", exist_ok=True)
        csv_path = "data/trades.csv"
        file_exists = os.path.isfile(csv_path)

        try:
            with open(csv_path, mode="a", newline="") as f:
                writer = csv.writer(f)
                if not file_exists:
                    # Write header
                    writer.writerow([
                        "exit_time",
                        "buy_symbol",
                        "sell_symbol",
                        "amount",
                        "entry_spread_pct",
                        "hold_time_sec",
                        "buy_side_pnl_usd",
                        "sell_side_pnl_usd",
                        "total_pnl_usd",
                    ])

                writer.writerow([
                    time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                    position.buy_symbol,
                    position.sell_symbol,
                    round(position.amount, 8),
                    round(position.entry_spread_pct, 4),
                    round(time.time() - position.entry_time, 0),
                    round(buy_side_pnl, 4),
                    round(sell_side_pnl, 4),
                    round(total_pnl, 4),
                ])
        except Exception as e:
            self.logger.error(f"Failed to write to CSV: {e}")

