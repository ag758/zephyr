"""
Cross-pair spread arbitrage strategy engine.

Monitors price discrepancies between correlated trading pairs
(e.g., BTC/USD vs BTC/USDT) and generates entry/exit signals
when the spread exceeds configurable thresholds.

This is a spot-only strategy — no futures or margin required.
Fully legal for US Kraken users.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.config import BotConfig
from src.utils.logger import setup_logger, log_with_data


@dataclass
class OrderBookSnapshot:
    """Stores the latest best bid/ask from an order book."""
    symbol: str
    best_bid: float = 0.0
    best_ask: float = 0.0
    best_bid_size: float = 0.0
    best_ask_size: float = 0.0
    timestamp: float = 0.0

    @property
    def mid_price(self) -> float:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2.0
        return 0.0

    @property
    def spread_pct(self) -> float:
        if self.best_bid:
            return ((self.best_ask - self.best_bid) / self.best_bid) * 100
        return 0.0


@dataclass
class ArbitragePosition:
    """Tracks an open cross-pair spread arbitrage position."""
    buy_symbol: str               # The symbol we bought (the cheaper one)
    sell_symbol: str               # The symbol we sold (the more expensive one)
    amount: float                  # Base asset quantity
    buy_entry_price: float         # Price paid on the buy side
    sell_entry_price: float        # Price received on the sell side
    entry_spread_pct: float        # Spread at entry
    entry_time: float              # Unix timestamp

    @property
    def entry_profit_usd(self) -> float:
        """Gross profit at entry (before fees)."""
        return (self.sell_entry_price - self.buy_entry_price) * self.amount

    @property
    def pair_key(self) -> str:
        """Unique key for this spread pair (sorted for consistency)."""
        symbols = sorted([self.buy_symbol, self.sell_symbol])
        return f"{symbols[0]}:{symbols[1]}"


@dataclass
class SpreadSignal:
    """Represents a detected spread opportunity or exit signal."""
    symbol_a: str                 # First symbol in the pair
    symbol_b: str                 # Second symbol in the pair
    buy_symbol: str               # Which side is cheaper (buy this)
    sell_symbol: str               # Which side is more expensive (sell this)
    buy_ask: float                # Cost to buy (ask price of cheap side)
    sell_bid: float               # Revenue from selling (bid price of expensive side)
    spread_usd: float             # Absolute spread in USD
    spread_pct: float             # Spread as % of buy price
    net_of_fees_pct: float        # After deducting round-trip fees
    is_entry: bool                # True if this is an entry signal
    timestamp: float


class StrategyEngine:
    """
    Core arbitrage logic for cross-pair spread trades.

    Monitors order books for correlated pairs, calculates spreads,
    generates entry/exit signals, and tracks positions.
    """

    def __init__(self, config: BotConfig):
        self.config = config
        self.logger = setup_logger("zephyr.strategy", config.log_level)

        # Use 0 fees if ignore_fees is set
        self.taker_fee = 0.0 if config.ignore_fees else config.taker_fee

        # Latest order book data keyed by symbol
        self.books: Dict[str, OrderBookSnapshot] = {}

        # Open arbitrage positions
        self.positions: List[ArbitragePosition] = []

    def update_book(self, symbol: str, orderbook: Dict) -> None:
        """Update the latest order book snapshot for a symbol."""
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        if bids and asks:
            self.books[symbol] = OrderBookSnapshot(
                symbol=symbol,
                best_bid=bids[0][0],
                best_ask=asks[0][0],
                best_bid_size=bids[0][1],
                best_ask_size=asks[0][1],
                timestamp=time.time(),
            )

    def evaluate(self, symbol_a: str, symbol_b: str) -> Optional[SpreadSignal]:
        """
        Evaluate whether there's a profitable spread between two symbols.

        Checks both directions:
          - Buy A at ask, Sell B at bid
          - Buy B at ask, Sell A at bid
        Returns the signal for the profitable direction (if any).

        Returns:
            SpreadSignal if prices are available, None otherwise
        """
        book_a = self.books.get(symbol_a)
        book_b = self.books.get(symbol_b)

        if not book_a or not book_b:
            return None

        if book_a.best_ask <= 0 or book_b.best_ask <= 0:
            return None
        if book_a.best_bid <= 0 or book_b.best_bid <= 0:
            return None

        # Direction 1: Buy A (at ask), Sell B (at bid)
        spread_ab = book_b.best_bid - book_a.best_ask
        spread_ab_pct = (spread_ab / book_a.best_ask) * 100

        # Direction 2: Buy B (at ask), Sell A (at bid)
        spread_ba = book_a.best_bid - book_b.best_ask
        spread_ba_pct = (spread_ba / book_b.best_ask) * 100

        # Pick the more profitable direction
        if spread_ab_pct >= spread_ba_pct:
            buy_symbol = symbol_a
            sell_symbol = symbol_b
            buy_ask = book_a.best_ask
            sell_bid = book_b.best_bid
            spread_usd = spread_ab
            spread_pct = spread_ab_pct
        else:
            buy_symbol = symbol_b
            sell_symbol = symbol_a
            buy_ask = book_b.best_ask
            sell_bid = book_a.best_bid
            spread_usd = spread_ba
            spread_pct = spread_ba_pct

        # Deduct round-trip fees
        net_of_fees_pct = spread_pct - self.config.round_trip_fee_pct

        # Check for entry signal
        is_entry = (
            net_of_fees_pct >= self.config.min_spread_pct
            and not self._has_max_positions(symbol_a, symbol_b)
        )

        return SpreadSignal(
            symbol_a=symbol_a,
            symbol_b=symbol_b,
            buy_symbol=buy_symbol,
            sell_symbol=sell_symbol,
            buy_ask=buy_ask,
            sell_bid=sell_bid,
            spread_usd=spread_usd,
            spread_pct=spread_pct,
            net_of_fees_pct=net_of_fees_pct,
            is_entry=is_entry,
            timestamp=time.time(),
        )

    def check_exit_signals(self) -> List[ArbitragePosition]:
        """
        Check if any open positions should be closed.

        Exit conditions:
        - Spread has compressed below exit_spread_pct (captured enough profit)
        - Spread has inverted (the position is now losing money)
        """
        exits = []

        for position in self.positions:
            book_buy = self.books.get(position.buy_symbol)
            book_sell = self.books.get(position.sell_symbol)

            if not book_buy or not book_sell:
                continue

            # To close: sell the bought side at bid, buy back the sold side at ask
            # Close spread = (what we get for selling bought asset) - (what we pay to buy back sold asset)
            close_revenue = book_buy.best_bid   # Sell the asset we bought
            close_cost = book_sell.best_ask      # Buy back the asset we sold

            # Current spread from the perspective of the *closing* trade
            # If we entered buying A and selling B:
            #   Entry profit direction: sell_bid_B - buy_ask_A > 0
            #   Current close cost direction: we sell A at bid, buy B at ask
            #   Remaining spread = sell_bid_B(now) - buy_ask_A(now)
            #   But for exit, we want to know the current live spread in the
            #   same direction as entry
            current_live_spread = book_sell.best_bid - book_buy.best_ask
            current_spread_pct = (current_live_spread / book_buy.best_ask) * 100 if book_buy.best_ask > 0 else 0

            # Exit if spread has compressed enough
            if current_spread_pct <= self.config.exit_spread_pct:
                log_with_data(
                    self.logger, "info",
                    "Exit signal: spread compressed",
                    buy_symbol=position.buy_symbol,
                    sell_symbol=position.sell_symbol,
                    entry_spread_pct=round(position.entry_spread_pct, 4),
                    current_spread_pct=round(current_spread_pct, 4),
                )
                exits.append(position)
                continue

            # Exit if spread has inverted significantly (losing money)
            if current_spread_pct < -1.0:
                log_with_data(
                    self.logger, "warning",
                    "Exit signal: spread inverted (stop-loss)",
                    buy_symbol=position.buy_symbol,
                    sell_symbol=position.sell_symbol,
                    current_spread_pct=round(current_spread_pct, 4),
                )
                exits.append(position)

        return exits

    def add_position(self, position: ArbitragePosition) -> None:
        """Record a new arbitrage position."""
        self.positions.append(position)
        log_with_data(
            self.logger, "info",
            "Position opened",
            buy_symbol=position.buy_symbol,
            sell_symbol=position.sell_symbol,
            amount=position.amount,
            buy_price=position.buy_entry_price,
            sell_price=position.sell_entry_price,
            spread_pct=round(position.entry_spread_pct, 4),
        )

    def remove_position(self, position: ArbitragePosition) -> None:
        """Remove a closed position."""
        if position in self.positions:
            self.positions.remove(position)
            log_with_data(
                self.logger, "info",
                "Position closed",
                buy_symbol=position.buy_symbol,
                sell_symbol=position.sell_symbol,
                amount=position.amount,
            )

    def _has_max_positions(self, symbol_a: str, symbol_b: str) -> bool:
        """Check if we've reached max positions for a spread pair."""
        pair_key = ":".join(sorted([symbol_a, symbol_b]))
        count = sum(1 for p in self.positions if p.pair_key == pair_key)
        return count >= self.config.max_positions_per_pair
