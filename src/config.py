"""
Configuration dataclass for the market making bot.

Centralizes all runtime configuration, provides validation,
and computes derived values like fee estimates.

Designed for Kraken single-pair market making with inventory skewing.
"""

from dataclasses import dataclass, field
from typing import List


# Kraken fee schedule (as of 2025)
# Regular tier maker fee: 0.16%
DEFAULT_MAKER_FEE = 0.0016  # 0.16%


@dataclass
class BotConfig:
    """Runtime configuration for the market making bot."""

    # --- Exchange credentials ---
    api_key: str = ""
    api_secret: str = ""

    # --- Mode flags ---
    live: bool = False          # Execute real trades if True
    ignore_fees: bool = False   # Pretend fees are 0% for testing

    # --- Trading parameters ---
    # List of symbols to market make on
    symbols: List[str] = field(
        default_factory=lambda: ["SOL/USD", "DOGE/USD"]
    )

    trade_amount_usd: float = 100.0       # USD notional per limit order
    
    # Market Making Parameters
    maker_base_spread_pct: float = 0.5    # Base target spread (ask - bid) as a %
    inventory_risk_aversion: float = 0.1  # How aggressively to skew quotes based on inventory imbalance
    order_refresh_tolerance_pct: float = 0.05 # Re-quote if optimal price moves more than this % away from current order
    test_balance_usd: float = 10000.0   # Starting USD for paper trading simulation

    # --- Fee estimates ---
    maker_fee: float = DEFAULT_MAKER_FEE

    # --- Monitoring ---
    log_level: str = "INFO"
    heartbeat_interval_sec: int = 60  # Log a heartbeat every N seconds

    def validate(self) -> None:
        """Validate configuration. Raises ValueError on invalid config."""
        if self.live:
            if not self.api_key or not self.api_secret:
                raise ValueError(
                    "API key and secret are required for LIVE trading. "
                    "To run safely in TEST MODE, ensure the --live flag is NOT present in your .env or command."
                )

        if self.trade_amount_usd <= 0:
            raise ValueError("Trade amount must be positive.")

        if not self.symbols:
            raise ValueError("At least one symbol must be specified.")

        for symbol in self.symbols:
            if "/" not in symbol:
                raise ValueError(
                    f"Invalid symbol format '{symbol}'. Expected format: 'BTC/USD'"
                )

    @property
    def all_symbols(self) -> List[str]:
        """Get a flat list of all unique symbols to monitor."""
        return sorted(list(set(self.symbols)))

    def __str__(self) -> str:
        exec_str = "LIVE" if self.live else "TEST MODE"
        sym_str = ", ".join(self.symbols)
        return (
            f"BotConfig({exec_str} | "
            f"symbols=[{sym_str}] | "
            f"order_size=${self.trade_amount_usd} | "
            f"test_bal=${self.test_balance_usd} | "
            f"base_spread={self.maker_base_spread_pct}% | "
            f"risk_aversion={self.inventory_risk_aversion} | "
            f"maker_fee={round(self.maker_fee * 100, 4)}%)"
        )
