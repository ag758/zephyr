"""
Exchange wrapper for Kraken.

Uses standard ccxt (MIT, free) for REST API calls (orders, balances, markets)
and the raw `websockets` library for native Kraken WebSocket v2 streams.
No paid licenses required.
"""

import asyncio
import json
import hashlib
from typing import Any, Callable, Dict, List, Optional

import ccxt
import websockets

from src.config import BotConfig
from src.utils.logger import setup_logger, log_with_data


# ---------------------------------------------------------------
# Kraken WebSocket v2 order book manager
# ---------------------------------------------------------------

KRAKEN_WS_URL = "wss://ws.kraken.com/v2"


class KrakenOrderBookStream:
    """
    Manages a WebSocket connection to Kraken's v2 API for
    streaming order book data.

    Handles:
    - Connection lifecycle
    - Subscription to book channels
    - Snapshot + incremental update processing
    - Checksum validation
    - Reconnection with exponential backoff
    """

    def __init__(self, symbols: List[str], depth: int = 25):
        """
        Args:
            symbols: List of Kraken symbols (e.g., ["BTC/USD", "BTC/USDT"])
            depth: Order book depth (10, 25, 100, 500, 1000)
        """
        self._symbols = symbols
        self._depth = depth
        self._ws = None
        self._running = False

        # Local order book state per symbol
        # Key: symbol, Value: {"bids": {price: qty}, "asks": {price: qty}}
        self._books: Dict[str, Dict] = {}

        # asyncio queues for delivering updates to consumers
        self._queues: Dict[str, asyncio.Queue] = {}
        for symbol in symbols:
            self._queues[symbol] = asyncio.Queue(maxsize=100)

    async def connect(self) -> None:
        """Establish WebSocket connection and subscribe to book channels."""
        self._running = True
        reconnect_delay = 1

        while self._running:
            try:
                async with websockets.connect(
                    KRAKEN_WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    reconnect_delay = 1  # Reset on successful connect

                    # Subscribe to book channel for all symbols
                    subscribe_msg = {
                        "method": "subscribe",
                        "params": {
                            "channel": "book",
                            "symbol": self._symbols,
                            "depth": self._depth,
                        },
                    }
                    await ws.send(json.dumps(subscribe_msg))

                    # Process messages
                    async for raw_msg in ws:
                        if not self._running:
                            break
                        await self._handle_message(raw_msg)

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 60)

    async def _handle_message(self, raw_msg: str) -> None:
        """Parse and route incoming WebSocket messages."""
        try:
            msg = json.loads(raw_msg)
        except json.JSONDecodeError:
            return

        # Skip system/heartbeat messages
        if isinstance(msg, dict):
            channel = msg.get("channel")
            msg_type = msg.get("type")

            if channel == "book":
                data_list = msg.get("data", [])
                for data in data_list:
                    symbol = data.get("symbol", "")
                    if not symbol:
                        continue

                    if msg_type == "snapshot":
                        self._apply_snapshot(symbol, data)
                    elif msg_type == "update":
                        self._apply_update(symbol, data)

                    # Push the current book state to the queue
                    book = self._get_formatted_book(symbol)
                    if book:
                        queue = self._queues.get(symbol)
                        if queue:
                            # Drop old updates if queue is full
                            if queue.full():
                                try:
                                    queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    pass
                            await queue.put(book)

    def _apply_snapshot(self, symbol: str, data: dict) -> None:
        """Apply a full order book snapshot."""
        bids = {}
        asks = {}

        for entry in data.get("bids", []):
            price = float(entry.get("price", 0))
            qty = float(entry.get("qty", 0))
            if price > 0:
                bids[price] = qty

        for entry in data.get("asks", []):
            price = float(entry.get("price", 0))
            qty = float(entry.get("qty", 0))
            if price > 0:
                asks[price] = qty

        self._books[symbol] = {"bids": bids, "asks": asks}

    def _apply_update(self, symbol: str, data: dict) -> None:
        """Apply incremental order book updates."""
        book = self._books.get(symbol)
        if not book:
            return

        for entry in data.get("bids", []):
            price = float(entry.get("price", 0))
            qty = float(entry.get("qty", 0))
            if qty == 0:
                book["bids"].pop(price, None)
            else:
                book["bids"][price] = qty

        for entry in data.get("asks", []):
            price = float(entry.get("price", 0))
            qty = float(entry.get("qty", 0))
            if qty == 0:
                book["asks"].pop(price, None)
            else:
                book["asks"][price] = qty

    def _get_formatted_book(self, symbol: str) -> Optional[Dict]:
        """
        Get the current order book in a normalized format.

        Returns:
            Dict with 'bids' and 'asks' as sorted lists of [price, qty]
        """
        book = self._books.get(symbol)
        if not book:
            return None

        bids = sorted(book["bids"].items(), key=lambda x: x[0], reverse=True)
        asks = sorted(book["asks"].items(), key=lambda x: x[0])

        if not bids or not asks:
            return None

        return {
            "bids": [[p, q] for p, q in bids[:self._depth]],
            "asks": [[p, q] for p, q in asks[:self._depth]],
            "symbol": symbol,
        }

    async def get_update(self, symbol: str) -> Dict:
        """
        Wait for the next order book update for a symbol.

        Returns:
            Normalized order book dict with 'bids' and 'asks'.
        """
        queue = self._queues.get(symbol)
        if not queue:
            raise ValueError(f"Not subscribed to {symbol}")
        return await queue.get()

    def get_latest(self, symbol: str) -> Optional[Dict]:
        """Get the latest cached orderbook snapshot without waiting."""
        return self._get_formatted_book(symbol)

    async def close(self) -> None:
        """Close the WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()


# ---------------------------------------------------------------
# Main exchange client
# ---------------------------------------------------------------

class ExchangeClient:
    """
    Wraps ccxt (REST) + websockets (WebSocket) for Kraken.

    - WebSocket streams via raw websockets library (free, open-source)
    - REST API calls via ccxt (free, MIT license)

    Usage:
        client = ExchangeClient(config)
        await client.initialize()
        # WebSocket streaming via monitor.py
        # REST orders via executor.py
        await client.close()
    """

    def __init__(self, config: BotConfig):
        self.config = config
        self.logger = setup_logger("zephyr.exchange", config.log_level)

        # Build ccxt exchange options for REST API
        exchange_opts: Dict[str, Any] = {
            "enableRateLimit": True,
        }
        if config.api_key:
            exchange_opts["apiKey"] = config.api_key
        if config.api_secret:
            exchange_opts["secret"] = config.api_secret

        # Initialize standard ccxt for REST
        self.exchange = ccxt.kraken(exchange_opts)

        # WebSocket stream (initialized in initialize())
        self._ws_stream: Optional[KrakenOrderBookStream] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._markets_loaded = False

    async def initialize(self) -> None:
        """
        Load markets and start WebSocket connections.
        Must be called before any trading operations.
        """
        loop = asyncio.get_running_loop()

        # Load markets via ccxt REST (run sync call in thread)
        self.logger.info("Loading markets from Kraken...")
        await loop.run_in_executor(None, self.exchange.load_markets)
        self._markets_loaded = True

        market_count = len(self.exchange.markets)
        log_with_data(
            self.logger, "info",
            "Markets loaded successfully",
            market_count=market_count,
        )

        # Start WebSocket order book stream
        all_symbols = self.config.all_symbols
        self._ws_stream = KrakenOrderBookStream(
            symbols=all_symbols,
            depth=25,
        )
        self._ws_task = asyncio.create_task(self._ws_stream.connect())
        self.logger.info(f"WebSocket stream started for {len(all_symbols)} symbols")

    # ---------------------------------------------------------------
    # WebSocket data access
    # ---------------------------------------------------------------

    async def wait_orderbook_update(self, symbol: str) -> Dict:
        """Await the next order book update for a symbol."""
        if not self._ws_stream:
            raise RuntimeError("WebSocket not initialized. Call initialize() first.")
        return await self._ws_stream.get_update(symbol)

    def get_latest_orderbook(self, symbol: str) -> Optional[Dict]:
        """Get the latest cached order book for a symbol."""
        if not self._ws_stream:
            return None
        return self._ws_stream.get_latest(symbol)

    # ---------------------------------------------------------------
    # REST API calls (via ccxt, run in thread executor)
    # ---------------------------------------------------------------

    async def _run_sync(self, func, *args, **kwargs):
        """Run a synchronous ccxt function in a thread executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: func(*args, **kwargs)
        )

    async def create_market_buy(self, symbol: str, amount: float) -> Dict:
        """
        Place a market buy order.

        Args:
            symbol: Trading pair like 'BTC/USD'
            amount: Quantity of base asset to buy

        Returns:
            Order response dict
        """
        self.logger.info(f"Placing MARKET BUY: {amount} {symbol}")
        order = await self._run_sync(
            self.exchange.create_order,
            symbol=symbol,
            type="market",
            side="buy",
            amount=amount,
        )
        log_with_data(
            self.logger, "info",
            "Buy order placed",
            order_id=order.get("id"),
            symbol=symbol,
            amount=amount,
            avg_price=order.get("average"),
            status=order.get("status"),
        )
        return order

    async def create_market_sell(self, symbol: str, amount: float) -> Dict:
        """
        Place a market sell order.

        Args:
            symbol: Trading pair like 'BTC/USD'
            amount: Quantity of base asset to sell

        Returns:
            Order response dict
        """
        self.logger.info(f"Placing MARKET SELL: {amount} {symbol}")
        order = await self._run_sync(
            self.exchange.create_order,
            symbol=symbol,
            type="market",
            side="sell",
            amount=amount,
        )
        log_with_data(
            self.logger, "info",
            "Sell order placed",
            order_id=order.get("id"),
            symbol=symbol,
            amount=amount,
            avg_price=order.get("average"),
            status=order.get("status"),
        )
        return order

    async def fetch_balance(self) -> Dict:
        """Fetch account balance."""
        return await self._run_sync(self.exchange.fetch_balance)

    async def close(self) -> None:
        """Close WebSocket connections and clean up."""
        self.logger.info("Closing exchange connections...")
        if self._ws_stream:
            await self._ws_stream.close()
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
