"""
Unit tests for the cross-pair spread strategy engine.

Tests spread calculation, fee deduction,
entry/exit signal logic, and position tracking.
"""

import time
import pytest
from src.config import BotConfig
from src.strategy import (
    StrategyEngine,
    OrderBookSnapshot,
    ArbitragePosition,
    SpreadSignal,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_config(**overrides) -> BotConfig:
    """Create a test config with sensible defaults."""
    defaults = {
        "spread_pairs": [("BTC/USD", "BTC/USDT")],
        "min_spread_pct": 0.5,
        "trade_amount_usd": 100.0,
        "taker_fee": 0.0026,  # 0.26%
        "dry_run": True,
        "log_level": "ERROR",  # Suppress logs in tests
        "exit_spread_pct": 0.05,
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
# Spread Calculation Tests
# ---------------------------------------------------------------------------

class TestSpreadCalculation:
    def test_positive_spread_direction_ab(self):
        """Symbol B is more expensive than Symbol A."""
        config = make_config()
        engine = StrategyEngine(config)

        # A is cheap: 50000 bid / 50010 ask
        engine.update_book("BTC/USD", make_orderbook(50000, 50010))
        # B is expensive: 50500 bid / 50510 ask
        engine.update_book("BTC/USDT", make_orderbook(50500, 50510))

        signal = engine.evaluate("BTC/USD", "BTC/USDT")

        assert signal is not None
        assert signal.buy_symbol == "BTC/USD"
        assert signal.sell_symbol == "BTC/USDT"
        
        # spread = sell_bid - buy_ask = 50500 - 50010 = 490
        assert signal.spread_usd == pytest.approx(490.0, abs=0.01)
        assert signal.spread_pct > 0

    def test_positive_spread_direction_ba(self):
        """Symbol A is more expensive than Symbol B."""
        config = make_config()
        engine = StrategyEngine(config)

        # A is expensive: 50500 bid / 50510 ask
        engine.update_book("BTC/USD", make_orderbook(50500, 50510))
        # B is cheap: 50000 bid / 50010 ask
        engine.update_book("BTC/USDT", make_orderbook(50000, 50010))

        signal = engine.evaluate("BTC/USD", "BTC/USDT")

        assert signal is not None
        assert signal.buy_symbol == "BTC/USDT"
        assert signal.sell_symbol == "BTC/USD"
        
        # spread = sell_bid - buy_ask = 50500 - 50010 = 490
        assert signal.spread_usd == pytest.approx(490.0, abs=0.01)
        assert signal.spread_pct > 0

    def test_negative_spread(self):
        """No profitable spread in either direction."""
        config = make_config()
        engine = StrategyEngine(config)

        # Tight, overlapping markets
        engine.update_book("BTC/USD", make_orderbook(50000, 50010))
        engine.update_book("BTC/USDT", make_orderbook(49995, 50005))

        signal = engine.evaluate("BTC/USD", "BTC/USDT")

        assert signal is not None
        assert signal.spread_usd < 0
        assert signal.spread_pct < 0
        assert signal.is_entry is False

    def test_zero_spread(self):
        """When prices are identical, spread is negative (due to bid/ask difference)."""
        config = make_config()
        engine = StrategyEngine(config)

        engine.update_book("BTC/USD", make_orderbook(50000, 50010))
        engine.update_book("BTC/USDT", make_orderbook(50000, 50010))

        signal = engine.evaluate("BTC/USD", "BTC/USDT")

        assert signal is not None
        # buy at 50010, sell at 50000 -> -10 USD spread
        assert signal.spread_usd == pytest.approx(-10.0, abs=0.01)
        assert signal.is_entry is False


# ---------------------------------------------------------------------------
# Fee Deduction Tests
# ---------------------------------------------------------------------------

class TestFeeDeduction:
    def test_round_trip_fee(self):
        """Round-trip fee should include two taker fees."""
        config = make_config(taker_fee=0.0026)
        # 0.26% * 2 = 0.52%
        assert config.round_trip_fee_pct == pytest.approx(0.52, abs=0.001)

    def test_fees_reduce_signal(self):
        """Net % should be less than gross by the fee amount."""
        config = make_config(min_spread_pct=0.0) 
        engine = StrategyEngine(config)

        engine.update_book("BTC/USD", make_orderbook(50000, 50010))
        engine.update_book("BTC/USDT", make_orderbook(51000, 51010))

        signal = engine.evaluate("BTC/USD", "BTC/USDT")

        assert signal is not None
        assert signal.net_of_fees_pct < signal.spread_pct
        assert signal.spread_pct - signal.net_of_fees_pct == pytest.approx(
            config.round_trip_fee_pct, abs=0.001
        )


# ---------------------------------------------------------------------------
# Entry Signal Tests
# ---------------------------------------------------------------------------

class TestEntrySignals:
    def test_entry_above_threshold(self):
        """Should signal entry when net spread exceeds min_spread_pct."""
        config = make_config(min_spread_pct=0.1, taker_fee=0.001) # Net threshold 0.1%, fee 0.2% round trip
        engine = StrategyEngine(config)

        # > 1% spread 
        engine.update_book("BTC/USD", make_orderbook(50000, 50010))
        engine.update_book("BTC/USDT", make_orderbook(50600, 50610))

        signal = engine.evaluate("BTC/USD", "BTC/USDT")

        assert signal is not None
        assert signal.is_entry is True

    def test_no_entry_below_threshold(self):
        """Should NOT signal entry when spread is below threshold."""
        config = make_config(min_spread_pct=5.0)  # High threshold
        engine = StrategyEngine(config)

        engine.update_book("BTC/USD", make_orderbook(50000, 50010))
        engine.update_book("BTC/USDT", make_orderbook(50100, 50110))

        signal = engine.evaluate("BTC/USD", "BTC/USDT")

        assert signal is not None
        assert signal.is_entry is False

    def test_no_entry_at_max_positions(self):
        """Should NOT signal entry if max positions are already open."""
        config = make_config(min_spread_pct=0.1, max_positions_per_pair=1)
        engine = StrategyEngine(config)

        # Add an existing position
        engine.add_position(ArbitragePosition(
            buy_symbol="BTC/USD",
            sell_symbol="BTC/USDT",
            amount=0.001,
            buy_entry_price=50000,
            sell_entry_price=50500,
            entry_spread_pct=1.0,
            entry_time=time.time(),
        ))

        # Even with a great spread, should not trigger
        engine.update_book("BTC/USD", make_orderbook(50000, 50010))
        engine.update_book("BTC/USDT", make_orderbook(55000, 55010))

        signal = engine.evaluate("BTC/USD", "BTC/USDT")

        assert signal is not None
        assert signal.is_entry is False


# ---------------------------------------------------------------------------
# Exit Signal Tests
# ---------------------------------------------------------------------------

class TestExitSignals:
    def test_exit_on_spread_compression(self):
        config = make_config(exit_spread_pct=0.05)
        engine = StrategyEngine(config)
        
        position = ArbitragePosition(
            buy_symbol="BTC/USD",
            sell_symbol="BTC/USDT",
            amount=0.001,
            buy_entry_price=50000,
            sell_entry_price=50500,
            entry_spread_pct=1.0,
            entry_time=time.time(),
        )
        engine.add_position(position)
        
        # Spread is completely closed (prices are identical)
        engine.update_book("BTC/USD", make_orderbook(50000, 50010))
        engine.update_book("BTC/USDT", make_orderbook(50000, 50010))
        
        exits = engine.check_exit_signals()
        assert len(exits) == 1
        assert exits[0] == position
        
    def test_exit_on_spread_inversion(self):
        config = make_config()
        engine = StrategyEngine(config)
        
        position = ArbitragePosition(
            buy_symbol="BTC/USD",
            sell_symbol="BTC/USDT",
            amount=0.001,
            buy_entry_price=50000,
            sell_entry_price=50500,
            entry_spread_pct=1.0,
            entry_time=time.time(),
        )
        engine.add_position(position)
        
        # Spread has inverted completely (stop loss scenario)
        engine.update_book("BTC/USD", make_orderbook(52000, 52010))
        engine.update_book("BTC/USDT", make_orderbook(50000, 50010))
        
        exits = engine.check_exit_signals()
        assert len(exits) == 1


# ---------------------------------------------------------------------------
# Position Tracking Tests
# ---------------------------------------------------------------------------

class TestPositionTracking:
    def test_add_remove_position(self):
        config = make_config()
        engine = StrategyEngine(config)

        position = ArbitragePosition(
            buy_symbol="BTC/USD",
            sell_symbol="BTC/USDT",
            amount=0.002,
            buy_entry_price=50000,
            sell_entry_price=50500,
            entry_spread_pct=1.0,
            entry_time=time.time(),
        )
        engine.add_position(position)

        assert len(engine.positions) == 1
        assert engine.positions[0].buy_entry_price == 50000
        
        engine.remove_position(position)
        assert len(engine.positions) == 0

    def test_entry_profit_usd(self):
        position = ArbitragePosition(
            buy_symbol="BTC/USD",
            sell_symbol="BTC/USDT",
            amount=0.1,
            buy_entry_price=50000,
            sell_entry_price=50500,
            entry_spread_pct=1.0,
            entry_time=time.time(),
        )
        # profit_usd = (50500 - 50000) * 0.1 = 50.0
        assert position.entry_profit_usd == pytest.approx(50.0, abs=0.01)


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_no_data_returns_none(self):
        """Evaluate should return None when no order book data exists."""
        config = make_config()
        engine = StrategyEngine(config)

        signal = engine.evaluate("BTC/USD", "BTC/USDT")
        assert signal is None

    def test_partial_data_returns_none(self):
        """Evaluate should return None when only one side has data."""
        config = make_config()
        engine = StrategyEngine(config)

        engine.update_book("BTC/USD", make_orderbook(50000, 50010))
        # No BTC/USDT data

        signal = engine.evaluate("BTC/USD", "BTC/USDT")
        assert signal is None

    def test_empty_orderbook_handled(self):
        """Empty orderbook should not crash."""
        config = make_config()
        engine = StrategyEngine(config)

        engine.update_book("BTC/USD", {"bids": [], "asks": []})

        assert "BTC/USD" not in engine.books

    def test_orderbook_snapshot_properties(self):
        """Test OrderBookSnapshot computed properties."""
        snap = OrderBookSnapshot(
            symbol="BTC/USD",
            best_bid=50000,
            best_ask=50100,
            best_bid_size=1.5,
            best_ask_size=2.0,
            timestamp=time.time(),
        )

        assert snap.mid_price == pytest.approx(50050.0, abs=0.01)
        assert snap.spread_pct == pytest.approx(0.2, abs=0.01)  # 100/50000 * 100


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
        config = make_config(spread_pairs=[("BTCUSD", "BTC/USDT")])
        with pytest.raises(ValueError, match="Invalid symbol format"):
            config.validate()
