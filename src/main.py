"""
Zephyr — Cross-Pair Spread Arbitrage Bot

Entry point with CLI argument parsing.
Initializes the exchange, strategy, executor, and monitor,
then launches the async WebSocket event loop.
"""

import argparse
import asyncio
import signal
import sys
from typing import List, Tuple

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
            "Cross-pair spread arbitrage bot for Kraken using CCXT and websockets. "
            "Monitors mispricing between correlated pairs and auto-executes spot-only trades."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Monitor-only (no trades)
  python -m src.main --dry-run --pairs BTC/USD:BTC/USDT,ETH/USD:ETH/USDT

  # Production (REAL MONEY — use with caution)
  python -m src.main --api-key KEY --api-secret SECRET --pairs BTC/USD:BTC/USDT
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
        "--dry-run",
        action="store_true",
        default=False,
        help="Monitor and log opportunities without placing any orders.",
    )

    parser.add_argument(
        "--ignore-fees",
        action="store_true",
        default=False,
        help="Pretend fees are 0% for testing purposes.",
    )

    # --- Trading parameters ---
    parser.add_argument(
        "--pairs",
        type=str,
        default="BTC/USD:BTC/USDT,ETH/USD:ETH/USDT",
        help="Comma-separated spread pairs to monitor, colon separated within pair. Default: BTC/USD:BTC/USDT,ETH/USD:ETH/USDT",
    )
    parser.add_argument(
        "--trade-amount-usd",
        type=float,
        default=100.0,
        help="USD notional amount per trade leg. Default: 100",
    )
    parser.add_argument(
        "--min-spread-pct",
        type=float,
        default=0.5,
        help="Minimum spread %% (after fees) to trigger entry. Default: 0.5",
    )
    parser.add_argument(
        "--max-positions",
        type=int,
        default=1,
        help="Maximum concurrent arbitrage positions per pair. Default: 1",
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
    """Convert parsed CLI arguments to a BotConfig."""
    spread_pairs: List[Tuple[str, str]] = []
    pairs_str = [p.strip() for p in args.pairs.split(",") if p.strip()]
    for p in pairs_str:
        parts = p.split(":")
        if len(parts) == 2:
            spread_pairs.append((parts[0].strip(), parts[1].strip()))

    return BotConfig(
        api_key=args.api_key,
        api_secret=args.api_secret,
        dry_run=args.dry_run,
        ignore_fees=args.ignore_fees,
        spread_pairs=spread_pairs,
        trade_amount_usd=args.trade_amount_usd,
        min_spread_pct=args.min_spread_pct,
        max_positions_per_pair=args.max_positions,
        log_level=args.log_level,
    )


async def run(config: BotConfig) -> None:
    """Main async entry point. Sets up all components and runs the monitor."""
    logger = setup_logger("zephyr", config.log_level)

    # Print banner
    logger.info("=" * 60)
    logger.info("  ⚡ ZEPHYR — Cross-Pair Spread Arbitrage Bot")
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

        # Log final status
        if strategy.positions:
            log_with_data(
                logger, "warning",
                "Shutting down with open positions!",
                open_positions=len(strategy.positions),
                positions=[
                    {
                        "buy_symbol": p.buy_symbol,
                        "sell_symbol": p.sell_symbol,
                        "amount": p.amount,
                    }
                    for p in strategy.positions
                ],
            )
        else:
            logger.info("Shutdown complete. No open positions.")

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
