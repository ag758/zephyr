# ⚡ ZEPHYR — Market Making Bot

Zephyr is a production-ready market-making engine for Kraken, optimized for high-volatility assets like PEPE. It uses an advanced Avellaneda-Stoikov model with trend filtering and non-linear inventory skew to provide liquidity while minimizing risk.

## 🚀 Quick Start

1. **Clone the Repo**:
   ```bash
   git clone https://github.com/your-repo/zephyr.git
   cd zephyr
   ```

2. **Setup Credentials**:
   ```bash
   cp .env.example .env
   # Open .env and add your Kraken keys (if going live)
   ```

3. **Configure the Bot**:
   Open the `.env` file and configure your strategy. You do **not** need to edit `docker-compose.yml`.

#### Option A: TEST MODE (Default)
Use this to simulate trades with paper money. No API keys required.
```bash
# API keys are optional in test mode
ZEPHYR_API_KEY=
ZEPHYR_API_SECRET=

# Mode & Symbols
ZEPHYR_LIVE=False
ZEPHYR_SYMBOLS=PEPE/USD
ZEPHYR_TEST_BALANCE=500.0

# Strategy
ZEPHYR_TRADE_AMOUNT=10.0
ZEPHYR_BASE_SPREAD=0.4
ZEPHYR_RISK_AVERSION=0.05
ZEPHYR_REFRESH_TOLERANCE=0.05
ZEPHYR_MAKER_FEE=0.16
```

#### Option B: LIVE MODE
Use this for real trading on your Lightsail instance.
```bash
# Your real Kraken keys
ZEPHYR_API_KEY=your_actual_key
ZEPHYR_API_SECRET=your_actual_secret

# Mode & Symbols
ZEPHYR_LIVE=True
ZEPHYR_SYMBOLS=PEPE/USD

# Strategy
ZEPHYR_TRADE_AMOUNT=10.0
ZEPHYR_BASE_SPREAD=0.4
ZEPHYR_RISK_AVERSION=0.05
ZEPHYR_REFRESH_TOLERANCE=0.05
ZEPHYR_MAKER_FEE=0.16
```

### 4. Run in the Background
```bash
docker compose up --build -d
```

### 5. Monitor the Bot
```bash
docker compose logs -f
```

## 💎 Recommended Production Settings (PEPE/USD)

| Variable | Recommended | Reason |
|----------|-------------|--------|
| `ZEPHYR_LIVE` | `True` | Enables real order execution. |
| `ZEPHYR_TRADE_AMOUNT` | `10.0` | Small size ($10) is better for high-volatility memecoins. |
| `ZEPHYR_BASE_SPREAD` | `0.4` | Covers 0.32% fees + leaves room for net profit. |
| `ZEPHYR_RISK_AVERSION` | `0.05` | Lower risk aversion prevents "panic" rebalancing. |
| `ZEPHYR_REFRESH_TOLERANCE` | `0.05` | Keeps orders tight with the market. |
| `ZEPHYR_MAKER_FEE` | `0.16` | Standard Kraken retail fee. |

## 📊 Trade Ledger
All trades (real or simulated) are logged to `data/trades.csv`. You can monitor your performance here.

## 🧪 Development
To run tests locally:
```bash
python -m pytest tests/ -v
```
