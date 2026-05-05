"""
Unit tests for the market making strategy engine.

Tests order book mid-price calculation, inventory skew logic,
reservation price calculation, and config validation.
"""

import time
import pytest
from src.config import BotConfig
from src.strategy import (
    StrategyEngine,
    OrderBookSnapshot,
    MarketMakerSignal,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_config(**overrides) -> BotConfig:
    """Create a test config with sensible defaults."""
    defaults = {
        "symbols": ["SOL/USD"],
        "trade_amount_usd": 100.0,
        "maker_base_spread_pct": 0.5,
        "inventory_risk_aversion": 0.1,
        "order_refresh_tolerance_pct": 0.05,
        "dry_run": True,
        "log_level": "ERROR",  # Suppress logs in tests
    }
    defaults.update(overrides)
    return BotConfig(**defaults)


def make_orderbook(best_bid: float, best_ask: float) -> dict:
    """Create a mock orderbook dict."""
    return {
        "bids": [[best_bid, 1.0]],
        "asks": [[best_ask, 1.0]],
    }


# ---------------------------------------------------------------------------
# OrderBook Snapshot Tests
# ---------------------------------------------------------------------------

class TestOrderBookSnapshot:
    def test_mid_price(self):
        snap = OrderBookSnapshot(
            symbol="SOL/USD",
            best_bid=100.0,
            best_ask=102.0,
        )
        assert snap.mid_price == 101.0

    def test_mid_price_missing_data(self):
        snap = OrderBookSnapshot(symbol="SOL/USD", best_bid=100.0, best_ask=0.0)
        assert snap.mid_price == 0.0


# ---------------------------------------------------------------------------
# Inventory Skew Tests
# ---------------------------------------------------------------------------

class TestInventorySkew:
    def test_perfect_balance(self):
        """When base ratio is exactly 50%, reservation price equals mid price."""
        config = make_config(inventory_risk_aversion=0.1)
        engine = StrategyEngine(config)

        engine.update_book("SOL/USD", make_orderbook(99.0, 101.0))  # Mid = 100.0
        
        # 1 SOL * 100 USD/SOL = $100 base value.
        # $100 quote balance. Total portfolio = $200. Ratio = 50%.
        signal = engine.evaluate("SOL/USD", base_balance=1.0, quote_balance=100.0)

        assert signal is not None
        assert signal.inventory_delta == pytest.approx(0.0)
        assert signal.reservation_price == pytest.approx(100.0)

    def test_long_base_asset(self):
        """When holding too much base asset, reservation price should be skewed lower."""
        config = make_config(inventory_risk_aversion=0.1)
        engine = StrategyEngine(config)

        engine.update_book("SOL/USD", make_orderbook(99.0, 101.0))  # Mid = 100.0
        
        # 2 SOL * 100 USD/SOL = $200 base value.
        # $0 quote balance. Total portfolio = $200. Ratio = 100%. Target is 50%.
        # Inventory delta = (1.0 - 0.5) / 0.5 = 1.0
        # Skew = 1.0 * 0.1 = 0.1 (capped at 0.05)
        # Reservation price = 100.0 * (1.0 - 0.05) = 95.0
        signal = engine.evaluate("SOL/USD", base_balance=2.0, quote_balance=0.0)

        assert signal is not None
        assert signal.inventory_delta == pytest.approx(1.0)
        assert signal.reservation_price == pytest.approx(95.0)

    def test_short_base_asset(self):
        """When holding too little base asset, reservation price should be skewed higher."""
        config = make_config(inventory_risk_aversion=0.1)
        engine = StrategyEngine(config)

        engine.update_book("SOL/USD", make_orderbook(99.0, 101.0))  # Mid = 100.0
        
        # 0 SOL * 100 USD/SOL = $0 base value.
        # $200 quote balance. Total portfolio = $200. Ratio = 0%. Target is 50%.
        # Inventory delta = (0.0 - 0.5) / 0.5 = -1.0
        # Skew = -1.0 * 0.1 = -0.1 (capped at -0.05)
        # Reservation price = 100.0 * (1.0 - -0.05) = 105.0
        signal = engine.evaluate("SOL/USD", base_balance=0.0, quote_balance=200.0)

        assert signal is not None
        assert signal.inventory_delta == pytest.approx(-1.0)
        assert signal.reservation_price == pytest.approx(105.0)
        
    def test_skew_capping(self):
        """Skew should be capped at 5% maximum to prevent extreme quotes."""
        # Risk aversion is very high
        config = make_config(inventory_risk_aversion=1.0)
        engine = StrategyEngine(config)

        engine.update_book("SOL/USD", make_orderbook(99.0, 101.0))  # Mid = 100.0
        
        # Inventory delta = 1.0
        # Skew would normally be 1.0 * 1.0 = 1.0, but capped at 0.05
        # Reservation price = 100.0 * (1.0 - 0.05) = 95.0
        signal = engine.evaluate("SOL/USD", base_balance=2.0, quote_balance=0.0)

        assert signal is not None
        assert signal.reservation_price == pytest.approx(95.0)


# ---------------------------------------------------------------------------
# Pricing Engine Tests
# ---------------------------------------------------------------------------

class TestPricingEngine:
    def test_base_spread_application(self):
        """Buy and sell prices should be equidistant from the reservation price."""
        # 2% maker spread -> half spread is 1%
        config = make_config(maker_base_spread_pct=2.0, inventory_risk_aversion=0.0)
        engine = StrategyEngine(config)

        # Mid = 100.0
        engine.update_book("SOL/USD", make_orderbook(99.0, 101.0))
        signal = engine.evaluate("SOL/USD", base_balance=1.0, quote_balance=100.0)

        assert signal is not None
        assert signal.reservation_price == pytest.approx(100.0)
        
        # Since reservation is 100, and half spread is 1%, buy should be 99, sell should be 101
        assert signal.buy_price == pytest.approx(99.0)
        assert signal.sell_price == pytest.approx(101.0)

    def test_passive_execution_capping(self):
        """Bot should never aggressively cross the order book."""
        # 0.2% maker spread -> half spread is 0.1%
        config = make_config(maker_base_spread_pct=0.2, inventory_risk_aversion=0.0)
        engine = StrategyEngine(config)

        # The book has a tight spread: 99.9 bid, 100.1 ask. Mid is 100.
        engine.update_book("SOL/USD", make_orderbook(99.9, 100.1))
        
        # Intended buy price: 100 * (1 - 0.001) = 99.9
        # Intended sell price: 100 * (1 + 0.001) = 100.1
        # This is exactly the book, which is fine.
        signal = engine.evaluate("SOL/USD", base_balance=1.0, quote_balance=100.0)
        assert signal.buy_price <= 100.1
        assert signal.sell_price >= 99.9
        
        # Now artificially skew it higher so that the intended buy price crosses the ask
        config.inventory_risk_aversion = 0.5
        # 0 balance, so it wants to buy aggressively.
        # Skew = -1 * 0.5 = -0.5. Capped at -0.05.
        # Reservation price = 105.
        # Buy price = 105 * 0.999 = 104.895
        # But best_ask is 100.1, so buy_price should be capped at 100.1 * 0.9999 = 100.08999
        signal = engine.evaluate("SOL/USD", base_balance=0.0, quote_balance=100.0)
        assert signal.reservation_price == pytest.approx(105.0)
        assert signal.buy_price == pytest.approx(100.1 * 0.9999)


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_no_data_returns_none(self):
        """Evaluate should return None when no order book data exists."""
        config = make_config()
        engine = StrategyEngine(config)

        signal = engine.evaluate("SOL/USD", base_balance=100.0, quote_balance=100.0)
        assert signal is None

    def test_empty_orderbook_handled(self):
        """Empty orderbook should not crash."""
        config = make_config()
        engine = StrategyEngine(config)

        engine.update_book("SOL/USD", {"bids": [], "asks": []})
        assert "SOL/USD" not in engine.books

    def test_zero_portfolio_value(self):
        """Should handle completely empty portfolio gracefully."""
        config = make_config()
        engine = StrategyEngine(config)

        engine.update_book("SOL/USD", make_orderbook(99.0, 101.0))
        
        # Both balances 0
        signal = engine.evaluate("SOL/USD", base_balance=0.0, quote_balance=0.0)
        assert signal is not None
        assert signal.inventory_delta == pytest.approx(-1.0) # Treats it as short base


# ---------------------------------------------------------------------------
# Config Validation Tests
# ---------------------------------------------------------------------------

class TestConfigValidation:
    def test_valid_config(self):
        config = make_config(api_key="key", api_secret="secret", dry_run=False)
        config.validate()  # Should not raise

    def test_missing_keys_in_live_mode(self):
        config = make_config(api_key="", api_secret="", dry_run=False)
        with pytest.raises(ValueError, match="API key and secret"):
            config.validate()

    def test_dry_run_no_keys_required(self):
        config = make_config(api_key="", api_secret="", dry_run=True)
        config.validate()  # Should not raise

    def test_invalid_symbol_format(self):
        config = make_config(symbols=["BTCUSD"])
        with pytest.raises(ValueError, match="Invalid symbol format"):
            config.validate()
            
    def test_negative_trade_amount(self):
        config = make_config(trade_amount_usd=-10)
        with pytest.raises(ValueError, match="positive"):
            config.validate()

