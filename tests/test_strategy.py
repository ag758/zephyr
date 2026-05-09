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
        "live": False,
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
        # In TEST MODE, buy_price is capped at best_ask (100.1)
        signal = engine.evaluate("SOL/USD", base_balance=0.0, quote_balance=100.0)
        assert signal.reservation_price == pytest.approx(105.0)
        assert signal.buy_price == pytest.approx(100.1)


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
        config = make_config(api_key="key", api_secret="secret", live=True)
        config.validate()  # Should not raise

    def test_missing_keys_in_live_mode(self):
        config = make_config(api_key="", api_secret="", live=True)
        with pytest.raises(ValueError, match="API key and secret"):
            config.validate()

    def test_test_mode_no_keys_required(self):
        config = make_config(api_key="", api_secret="", live=False)
        config.validate()  # Should not raise

    def test_invalid_symbol_format(self):
        config = make_config(symbols=["BTCUSD"])
        with pytest.raises(ValueError, match="Invalid symbol format"):
            config.validate()
            
    def test_negative_trade_amount(self):
        config = make_config(trade_amount_usd=-10)
        with pytest.raises(ValueError, match="positive"):
            config.validate()

    def test_empty_symbols_list(self):
        config = make_config(symbols=[])
        with pytest.raises(ValueError, match="At least one symbol"):
            config.validate()

    def test_config_str_shows_live(self):
        config = make_config(live=True, api_key="k", api_secret="s")
        assert "LIVE" in str(config)

    def test_config_str_shows_test_mode(self):
        config = make_config(live=False)
        assert "TEST MODE" in str(config)

    def test_all_symbols_deduplicates(self):
        config = make_config(symbols=["SOL/USD", "PEPE/USD", "SOL/USD"])
        assert config.all_symbols == ["PEPE/USD", "SOL/USD"]

    def test_pepe_symbol_valid(self):
        """PEPE/USD — the primary symbol we trade — should pass validation."""
        config = make_config(symbols=["PEPE/USD"])
        config.validate()  # Should not raise


# ---------------------------------------------------------------------------
# Live vs Test Mode Capping Tests
# ---------------------------------------------------------------------------

class TestLiveVsTestMode:
    """Tests the critical behavioral difference between live and test mode."""

    def test_live_mode_never_crosses_mid(self):
        """In LIVE mode, buy_price must stay below mid * 0.9995."""
        config = make_config(
            live=True, api_key="k", api_secret="s",
            inventory_risk_aversion=0.5, maker_base_spread_pct=0.2,
        )
        engine = StrategyEngine(config)
        engine.update_book("SOL/USD", make_orderbook(99.9, 100.1))  # Mid = 100.0

        # Aggressive skew: 0 base, wants to buy hard
        signal = engine.evaluate("SOL/USD", base_balance=0.0, quote_balance=200.0)
        assert signal is not None
        # Must stay at or below best_bid (99.9) in live mode
        assert signal.buy_price <= 99.9
        # Must stay at or above best_ask (100.1) in live mode
        assert signal.sell_price >= 100.1

    def test_test_mode_can_touch_ask(self):
        """In TEST mode, buy_price can reach best_ask to trigger simulated fills."""
        config = make_config(
            live=False,
            inventory_risk_aversion=0.5, maker_base_spread_pct=0.2,
        )
        engine = StrategyEngine(config)
        engine.update_book("SOL/USD", make_orderbook(99.9, 100.1))  # Mid = 100.0

        signal = engine.evaluate("SOL/USD", base_balance=0.0, quote_balance=200.0)
        assert signal is not None
        # In test mode, buy_price is capped at best_ask, not best_bid
        assert signal.buy_price == pytest.approx(100.1)

    def test_live_mode_sell_respects_ask(self):
        """In LIVE mode, sell_price must be >= best_ask."""
        config = make_config(
            live=True, api_key="k", api_secret="s",
            inventory_risk_aversion=0.5, maker_base_spread_pct=0.2,
        )
        engine = StrategyEngine(config)
        engine.update_book("SOL/USD", make_orderbook(99.9, 100.1))

        # Long base: wants to sell aggressively
        signal = engine.evaluate("SOL/USD", base_balance=2.0, quote_balance=0.0)
        assert signal is not None
        assert signal.sell_price >= 100.1


# ---------------------------------------------------------------------------
# Trend Filter Tests
# ---------------------------------------------------------------------------

class TestTrendFilter:
    """Tests the adverse selection protection (trend buy block)."""

    def test_trend_block_drops_buy_price(self):
        """When price drops >0.2% below SMA, buy price should drop to 50% of mid."""
        config = make_config(inventory_risk_aversion=0.0)
        engine = StrategyEngine(config)

        # Feed 60 price points at 100.0 to establish an SMA
        for _ in range(60):
            engine.update_book("SOL/USD", make_orderbook(99.5, 100.5))

        # Now drop the price sharply (mid = 99.0, which is < 100.0 * 0.998 = 99.8)
        engine.update_book("SOL/USD", make_orderbook(98.5, 99.5))
        signal = engine.evaluate("SOL/USD", base_balance=1.0, quote_balance=100.0)

        assert signal is not None
        # Buy price should be dropped to 50% of mid (extreme low)
        assert signal.buy_price == pytest.approx(99.0 * 0.5)

    def test_no_trend_block_when_price_stable(self):
        """No trend block when price stays near SMA."""
        config = make_config(inventory_risk_aversion=0.0, maker_base_spread_pct=1.0)
        engine = StrategyEngine(config)

        # Feed 60 identical price points
        for _ in range(61):
            engine.update_book("SOL/USD", make_orderbook(99.5, 100.5))

        signal = engine.evaluate("SOL/USD", base_balance=1.0, quote_balance=100.0)
        assert signal is not None
        # Buy price should be normal (not dropped to 50%)
        assert signal.buy_price > 99.0 * 0.9

    def test_trend_block_requires_60_data_points(self):
        """Trend filter should not activate with less than 60 data points."""
        config = make_config(inventory_risk_aversion=0.0, maker_base_spread_pct=1.0)
        engine = StrategyEngine(config)

        # Feed only 58 points at a high price, then drop (total = 59, below threshold)
        for _ in range(58):
            engine.update_book("SOL/USD", make_orderbook(99.5, 100.5))
        engine.update_book("SOL/USD", make_orderbook(90.0, 91.0))

        signal = engine.evaluate("SOL/USD", base_balance=1.0, quote_balance=100.0)
        assert signal is not None
        # Should NOT be blocked (only 59 entries, filter requires >= 60)
        assert signal.buy_price > 90.5 * 0.9


# ---------------------------------------------------------------------------
# Volatility-Adjusted Spread Tests
# ---------------------------------------------------------------------------

class TestVolatilitySpread:
    """Tests that spreads widen during high volatility."""

    def test_spread_widens_with_volatility(self):
        """High price range should produce wider spreads."""
        config = make_config(
            inventory_risk_aversion=0.0, maker_base_spread_pct=1.0,
        )
        engine = StrategyEngine(config)

        # Feed varied prices to create volatility (range = 10%)
        for price in [95.0, 100.0, 105.0] * 4:  # 12 points
            engine.update_book("SOL/USD", make_orderbook(price - 0.5, price + 0.5))

        signal = engine.evaluate("SOL/USD", base_balance=1.0, quote_balance=105.0)
        assert signal is not None

        # With high volatility, the effective spread should be > base spread
        spread_pct = (signal.sell_price - signal.buy_price) / signal.mid_price * 100
        assert spread_pct > 1.0  # Should be wider than the 1.0% base

    def test_no_vol_adjustment_with_few_points(self):
        """With < 10 data points, vol multiplier should be 1.0 (no adjustment)."""
        config = make_config(
            inventory_risk_aversion=0.0, maker_base_spread_pct=2.0,
        )
        engine = StrategyEngine(config)

        # Only 1 data point
        engine.update_book("SOL/USD", make_orderbook(99.0, 101.0))
        signal = engine.evaluate("SOL/USD", base_balance=1.0, quote_balance=100.0)

        assert signal is not None
        # With 2% spread and no vol adjustment: buy ~ 99, sell ~ 101
        assert signal.buy_price == pytest.approx(99.0)
        assert signal.sell_price == pytest.approx(101.0)


# ---------------------------------------------------------------------------
# Non-Linear Inventory Decay Tests
# ---------------------------------------------------------------------------

class TestNonLinearSkew:
    """Tests the delta * abs(delta) non-linear inventory decay curve."""

    def test_small_imbalance_has_minimal_skew(self):
        """A small imbalance (e.g., 60/40) should barely move the reservation price."""
        config = make_config(inventory_risk_aversion=0.1)
        engine = StrategyEngine(config)
        engine.update_book("SOL/USD", make_orderbook(99.0, 101.0))  # Mid = 100

        # 60% base: delta = (0.6 - 0.5) / 0.5 = 0.2
        # Non-linear skew = 0.2 * 0.2 * 0.1 * 2 = 0.008
        # Reservation = 100 * (1 - 0.008) = 99.2
        signal = engine.evaluate("SOL/USD", base_balance=1.5, quote_balance=100.0)
        assert signal is not None
        assert signal.reservation_price == pytest.approx(99.2, abs=0.1)

    def test_large_imbalance_has_steep_skew(self):
        """A large imbalance should hit the 5% cap."""
        config = make_config(inventory_risk_aversion=0.5)
        engine = StrategyEngine(config)
        engine.update_book("SOL/USD", make_orderbook(99.0, 101.0))  # Mid = 100

        # 100% base: delta = 1.0
        # Non-linear skew = 1.0 * 1.0 * 0.5 * 2 = 1.0, capped at 0.05
        signal = engine.evaluate("SOL/USD", base_balance=2.0, quote_balance=0.0)
        assert signal is not None
        assert signal.reservation_price == pytest.approx(95.0)


# ---------------------------------------------------------------------------
# Maker Fee Conversion Tests
# ---------------------------------------------------------------------------

class TestMakerFeeConversion:
    """Tests that the maker fee percentage is correctly converted."""

    def test_fee_stored_as_decimal(self):
        """ZEPHYR_MAKER_FEE=0.16 should be stored as 0.0016 internally."""
        # build_config divides by 100: 0.16 / 100 = 0.0016
        config = make_config(maker_fee=0.0016)
        assert config.maker_fee == pytest.approx(0.0016)

    def test_fee_in_config_str(self):
        """Config __str__ should display fee as a percentage."""
        config = make_config(maker_fee=0.0016)
        config_str = str(config)
        assert "0.16%" in config_str

