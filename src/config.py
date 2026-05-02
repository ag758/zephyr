"""
Configuration dataclass for the arbitrage bot.

Centralizes all runtime configuration, provides validation,
and computes derived values like fee estimates.

Designed for Kraken cross-pair spread arbitrage (spot-only, US-legal).
"""

from dataclasses import dataclass, field
from typing import List, Tuple


# Kraken fee schedule (as of 2025)
# Regular tier taker fee: 0.26% (decreases with volume)
# Maker fee: 0.16%
# We use taker fees as worst-case for market orders
DEFAULT_TAKER_FEE = 0.0026  # 0.26%


@dataclass
class BotConfig:
    """Runtime configuration for the cross-pair spread arbitrage bot."""

    # --- Exchange credentials ---
    api_key: str = ""
    api_secret: str = ""

    # --- Mode flags ---
    dry_run: bool = False       # Log only, don't execute trades
    ignore_fees: bool = False   # Pretend fees are 0% for testing

    # --- Trading parameters ---
    # Each spread pair is a tuple of (symbol_a, symbol_b) to monitor
    # e.g., ("BTC/USD", "BTC/USDT") — buy cheap side, sell expensive side
    spread_pairs: List[Tuple[str, str]] = field(
        default_factory=lambda: [("BTC/USD", "BTC/USDT"), ("ETH/USD", "ETH/USDT")]
    )

    trade_amount_usd: float = 100.0       # USD notional per trade leg
    min_spread_pct: float = 0.5           # Minimum spread % (after fees) to trigger entry
    max_positions_per_pair: int = 1       # Max concurrent arb positions per spread pair
    exit_spread_pct: float = 0.05         # Close when spread compresses to this %

    # --- Fee estimates ---
    taker_fee: float = DEFAULT_TAKER_FEE  # Per-leg taker fee

    # --- Monitoring ---
    log_level: str = "INFO"
    heartbeat_interval_sec: int = 60  # Log a heartbeat every N seconds

    def validate(self) -> None:
        """Validate configuration. Raises ValueError on invalid config."""
        if not self.dry_run:
            if not self.api_key or not self.api_secret:
                raise ValueError(
                    "API key and secret are required for live trading. "
                    "Use --dry-run for monitor-only mode."
                )

        if self.trade_amount_usd <= 0:
            raise ValueError("Trade amount must be positive.")

        if not self.spread_pairs:
            raise ValueError("At least one spread pair must be specified.")

        for pair in self.spread_pairs:
            if len(pair) != 2:
                raise ValueError(
                    f"Invalid spread pair {pair}. Each pair must have exactly 2 symbols."
                )
            for symbol in pair:
                if "/" not in symbol:
                    raise ValueError(
                        f"Invalid symbol format '{symbol}'. Expected format: 'BTC/USD'"
                    )

    @property
    def all_symbols(self) -> List[str]:
        """Get a flat list of all unique symbols to monitor."""
        symbols = set()
        for a, b in self.spread_pairs:
            symbols.add(a)
            symbols.add(b)
        return sorted(symbols)

    @property
    def round_trip_fee_pct(self) -> float:
        """
        Total estimated round-trip fee percentage.
        Includes: buy taker fee + sell taker fee (two spot legs).
        """
        if self.ignore_fees:
            return 0.0
        return self.taker_fee * 2 * 100

    def __str__(self) -> str:
        exec_str = "DRY-RUN" if self.dry_run else "LIVE"
        pairs_str = ", ".join(f"{a}↔{b}" for a, b in self.spread_pairs)
        return (
            f"BotConfig({exec_str} | "
            f"pairs=[{pairs_str}] | "
            f"trade_amount=${self.trade_amount_usd} | "
            f"min_spread={self.min_spread_pct}% | "
            f"round_trip_fees={self.round_trip_fee_pct:.3f}%)"
        )
