"""
Zephyr — Market Making Bot

Entry point with CLI argument parsing.
Initializes the exchange, strategy, executor, and monitor,
then launches the async WebSocket event loop.
"""

import argparse
import asyncio
import os
import signal
import sys
from typing import List

from src.config import BotConfig
from src.exchange import ExchangeClient
from src.strategy import StrategyEngine
from src.executor import TradeExecutor
from src.monitor import Monitor
from src.utils.logger import setup_logger, log_with_data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="zephyr",
        description=(
            "Inventory-skewed market making bot for Kraken using CCXT and websockets. "
            "Places limit orders around the mid-price to capture spread."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Test mode (paper trading, no real money)
  python -m src.main --symbols PEPE/USD --test-balance-usd 500

  # Production (REAL MONEY — use with caution)
  python -m src.main --live --api-key KEY --api-secret SECRET --symbols PEPE/USD --trade-amount-usd 10
        """,
    )

    # --- Exchange credentials ---
    parser.add_argument(
        "--api-key",
        type=str,
        default="",
        help="Kraken API key. Required for live trading.",
    )
    parser.add_argument(
        "--api-secret",
        type=str,
        default="",
        help="Kraken API secret. Required for live trading.",
    )

    # --- Mode flags ---
    parser.add_argument(
        "--live",
        action="store_true",
        help="Enable LIVE trading mode. If not passed, bot defaults to safe TEST MODE.",
    )
    parser.add_argument(
        "--test-balance-usd",
        type=float,
        default=10000.0,
        help="Initial USD balance for simulation (when NOT in live mode).",
    )

    parser.add_argument(
        "--ignore-fees",
        action="store_true",
        default=False,
        help="Pretend fees are 0 percent for testing purposes.",
    )

    # --- Trading parameters ---
    parser.add_argument(
        "--symbols",
        type=str,
        default="SOL/USD,DOGE/USD",
        help="Comma-separated symbols to market make on. Default: SOL/USD,DOGE/USD",
    )
    parser.add_argument(
        "--trade-amount-usd",
        type=float,
        default=100.0,
        help="USD notional amount per limit order. Default: 100",
    )
    parser.add_argument(
        "--maker-base-spread-pct",
        type=float,
        default=0.5,
        help="Base target spread (ask - bid) as a percent. Default: 0.5",
    )
    parser.add_argument(
        "--inventory-risk-aversion",
        type=float,
        default=0.1,
        help="How aggressively to skew quotes based on inventory imbalance. Default: 0.1",
    )
    parser.add_argument(
        "--order-refresh-tolerance-pct",
        type=float,
        default=0.05,
        help="Re-quote if optimal price moves more than this percent away from current order. Default: 0.05",
    )

    parser.add_argument(
        "--maker-fee-pct",
        type=float,
        default=0.16,
        help="Kraken maker fee as a percentage (e.g., 0.16 for 0.16 percent). Default: 0.16",
    )

    # --- Logging ---
    parser.add_argument(
        "--log-level",
        type=str,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log verbosity level. Default: INFO",
    )

    return parser.parse_args()


def build_config(args: argparse.Namespace) -> BotConfig:
    """
    Convert parsed CLI arguments to a BotConfig, with environment variable overrides.
    Priority: Environment Variable > CLI Argument > Default
    """
    def get_env_bool(name: str, default: bool) -> bool:
        val = os.getenv(name)
        if val is None: return default
        return val.lower() in ("true", "1", "yes")

    def get_env_float(name: str, default: float) -> float:
        val = os.getenv(name)
        return float(val) if val else default

    # Symbols: Environment variable ZEPHYR_SYMBOLS or CLI --symbols
    symbols_str = os.getenv("ZEPHYR_SYMBOLS", args.symbols)
    symbols = [s.strip() for s in symbols_str.split(",") if s.strip()]

    # Mode: Environment variable ZEPHYR_LIVE or CLI --live
    # Defaults to TEST MODE (False) if neither is set.
    live = get_env_bool("ZEPHYR_LIVE", args.live)
    
    return BotConfig(
        api_key=os.getenv("ZEPHYR_API_KEY", args.api_key),
        api_secret=os.getenv("ZEPHYR_API_SECRET", args.api_secret),
        live=live,
        ignore_fees=get_env_bool("ZEPHYR_IGNORE_FEES", args.ignore_fees),
        symbols=symbols,
        trade_amount_usd=get_env_float("ZEPHYR_TRADE_AMOUNT", args.trade_amount_usd),
        maker_base_spread_pct=get_env_float("ZEPHYR_BASE_SPREAD", args.maker_base_spread_pct),
        inventory_risk_aversion=get_env_float("ZEPHYR_RISK_AVERSION", args.inventory_risk_aversion),
        order_refresh_tolerance_pct=get_env_float("ZEPHYR_REFRESH_TOLERANCE", args.order_refresh_tolerance_pct),
        test_balance_usd=get_env_float("ZEPHYR_TEST_BALANCE", args.test_balance_usd),
        maker_fee=get_env_float("ZEPHYR_MAKER_FEE", args.maker_fee_pct) / 100.0,
        log_level=os.getenv("ZEPHYR_LOG_LEVEL", args.log_level),
    )


async def run(config: BotConfig) -> None:
    """Main async entry point. Sets up all components and runs the monitor."""
    logger = setup_logger("zephyr", config.log_level)

    # Print banner
    logger.info("=" * 60)
    logger.info("  ⚡ ZEPHYR — Market Making Bot")
    logger.info("=" * 60)
    logger.info(f"  Config: {config}")
    logger.info("=" * 60)

    # Initialize exchange client
    exchange = ExchangeClient(config)

    try:
        # Load markets and start WebSocket streams
        await exchange.initialize()

        # Set up strategy engine
        strategy = StrategyEngine(config)

        # Set up trade executor
        executor = TradeExecutor(config, exchange, strategy)

        # Set up WebSocket monitor
        monitor = Monitor(config, exchange, strategy, executor)

        # Handle graceful shutdown
        loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()

        def _signal_handler():
            logger.info("Shutdown signal received, stopping...")
            shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

        # Run monitor and shutdown watcher concurrently
        monitor_task = asyncio.create_task(monitor.start())
        shutdown_task = asyncio.create_task(shutdown_event.wait())

        # Wait for either the monitor to finish or a shutdown signal
        done, pending = await asyncio.wait(
            [monitor_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel remaining tasks
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
                
        # Optional: Cancel open orders on shutdown
        if config.live:
            logger.info("Cancelling open orders on shutdown...")
            for symbol, active in executor.active_orders.items():
                if active.buy_order_id:
                    try:
                        await exchange.cancel_order(active.buy_order_id, symbol)
                    except Exception as e:
                        logger.error(f"Failed to cancel buy order {active.buy_order_id}: {e}")
                if active.sell_order_id:
                    try:
                        await exchange.cancel_order(active.sell_order_id, symbol)
                    except Exception as e:
                        logger.error(f"Failed to cancel sell order {active.sell_order_id}: {e}")

        logger.info("Shutdown complete.")

    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise
    finally:
        await exchange.close()


def main():
    args = parse_args()
    config = build_config(args)

    try:
        config.validate()
    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
