"""
Market making strategy engine.

Calculates optimal bid and ask quotes based on an inventory-skewing
model (inspired by Avellaneda-Stoikov). Modifies quotes based on current
exposure to keep portfolio balanced.

This is a spot-only strategy.
"""

import collections
import time
from dataclasses import dataclass
from typing import Dict, Optional

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


@dataclass
class MarketMakerSignal:
    """Represents the desired optimal quotes for a symbol."""
    symbol: str
    mid_price: float
    reservation_price: float
    buy_price: float
    sell_price: float
    inventory_delta: float
    timestamp: float


class StrategyEngine:
    """
    Core logic for single-pair market making.

    Monitors order books, computes fair value, skews fair value
    based on inventory, and returns optimal limit order prices.
    """

    def __init__(self, config: BotConfig):
        self.config = config
        self.logger = setup_logger("zephyr.strategy", config.log_level)

        # Latest order book data keyed by symbol
        self.books: Dict[str, OrderBookSnapshot] = {}
        
        # Price history for volatility and trend calculations
        self.price_history: Dict[str, collections.deque] = collections.defaultdict(
            lambda: collections.deque(maxlen=120)  # Roughly 2 minutes at 1 tick/sec
        )

    def update_book(self, symbol: str, orderbook: Dict) -> None:
        """Update the latest order book snapshot for a symbol."""
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        if bids and asks:
            best_bid = bids[0][0]
            best_ask = asks[0][0]
            self.books[symbol] = OrderBookSnapshot(
                symbol=symbol,
                best_bid=best_bid,
                best_ask=best_ask,
                best_bid_size=bids[0][1],
                best_ask_size=asks[0][1],
                timestamp=time.time(),
            )
            # Store mid price history
            mid_price = (best_bid + best_ask) / 2.0
            self.price_history[symbol].append(mid_price)

    def evaluate(
        self,
        symbol: str,
        base_balance: float,
        quote_balance: float,
    ) -> Optional[MarketMakerSignal]:
        """
        Evaluate optimal market making quotes for a symbol.

        Args:
            symbol: Trading pair like 'BTC/USD'
            base_balance: Quantity of base asset held (e.g., BTC)
            quote_balance: Quantity of quote asset held (e.g., USD)

        Returns:
            MarketMakerSignal if valid orderbook, else None
        """
        book = self.books.get(symbol)
        if not book or book.best_bid <= 0 or book.best_ask <= 0:
            return None

        mid_price = book.mid_price
        history = self.price_history.get(symbol, [])
        
        # 1. Trend Filter (Adverse Selection Protection)
        # Prevent buying if there's a sharp downtrend.
        trend_buy_block = False
        if len(history) >= 60:
            sma = sum(history) / len(history)
            # If current price is >0.2% below recent average, it's a sharp drop
            if mid_price < sma * 0.998:
                trend_buy_block = True
                self.logger.warning(f"Trend block active for {symbol}. Mid: {mid_price}, SMA: {sma}")

        # 2. Volatility-Adjusted Spreads
        vol_multiplier = 1.0
        if len(history) >= 10:
            highest = max(history)
            lowest = min(history)
            volatility_pct = (highest - lowest) / mid_price
            # Scale spread. E.g., if range is 1%, volatility_pct is 0.01.
            # Base spread * (1 + 100 * 0.01) = Base * 2.
            vol_multiplier = max(1.0, 1.0 + (volatility_pct * 100.0))
            
        current_spread_pct = self.config.maker_base_spread_pct * vol_multiplier
        
        # Calculate portfolio value and current base asset ratio
        base_value_in_quote = base_balance * mid_price
        total_portfolio_value = base_value_in_quote + quote_balance

        if total_portfolio_value <= 0:
            current_base_ratio = 0.0
        else:
            current_base_ratio = base_value_in_quote / total_portfolio_value

        # Target 50% in base, 50% in quote for delta-neutral market making
        target_base_ratio = 0.5

        # Inventory delta ranges from roughly -1 (no base) to +1 (all base)
        if target_base_ratio > 0:
            inventory_delta = (current_base_ratio - target_base_ratio) / target_base_ratio
        else:
            inventory_delta = 0.0

        # 3. Non-Linear Inventory Decay
        # Use delta * abs(delta) to create a curve that is flat near 0 and steep near 1/-1.
        # Multiplied by 2 to ensure it still reaches a strong skew at extremes.
        skew = inventory_delta * abs(inventory_delta) * self.config.inventory_risk_aversion * 2.0
        
        # Cap the skew to avoid extreme quotes if balances are very imbalanced
        skew = max(min(skew, 0.05), -0.05) # Max 5% skew
        
        reservation_price = mid_price * (1.0 - skew)

        # Calculate Quotes
        half_spread_pct = current_spread_pct / 200.0
        
        buy_price = reservation_price * (1.0 - half_spread_pct)
        sell_price = reservation_price * (1.0 + half_spread_pct)

        # 4. Strict Passive Rebalancing
        # Ensure we NEVER cross the mid-price in LIVE mode to avoid taker fees.
        if self.config.live:
            buy_price = min(buy_price, mid_price * 0.9995)
            sell_price = max(sell_price, mid_price * 1.0005)
            
            # Also respect the actual order book
            buy_price = min(buy_price, book.best_bid)
            sell_price = max(sell_price, book.best_ask)
        else:
            # In TEST MODE, we allow the bot to "touch" the other side to trigger fills
            buy_price = min(buy_price, book.best_ask)
            sell_price = max(sell_price, book.best_bid)

        if trend_buy_block:
            # Drop the buy price extremely low so it doesn't get filled
            buy_price = mid_price * 0.5

        return MarketMakerSignal(
            symbol=symbol,
            mid_price=mid_price,
            reservation_price=reservation_price,
            buy_price=buy_price,
            sell_price=sell_price,
            inventory_delta=inventory_delta,
            timestamp=time.time(),
        )
