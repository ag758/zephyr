# ⚡ Zephyr — Cross-Pair Spread Arbitrage Bot

Zephyr is a high-performance, Dockerized trading bot that monitors and executes **cross-pair spread arbitrage** on the **Kraken** exchange.

Because futures and margin trading are heavily restricted for US residents, this bot is designed to be **100% US-legal**. It uses a spot-only strategy, trading mispricings between correlated pairs (like `BTC/USD` and `BTC/USDT`).

It uses open-source libraries (`ccxt` + `websockets`) to stream data via Kraken's WebSocket API v2 and execute dual-leg spot trades simultaneously.

## 🧠 Strategy: Cross-Pair Spread

Instead of trading spot against futures (which requires margin), Zephyr monitors two closely correlated spot pairs. 

For example, `BTC/USD` and `BTC/USDT` should technically have the exact same price (assuming 1 USD = 1 USDT). However, due to temporary liquidity imbalances on Kraken, their prices often diverge.

**How it works:**
1. **Monitor**: The bot streams live order book data for both pairs via WebSockets.
2. **Evaluate**: It calculates the spread in both directions:
   - Buy `BTC/USD`, Sell `BTC/USDT`
   - Buy `BTC/USDT`, Sell `BTC/USD`
3. **Execute (Entry)**: If the spread exceeds the `min-spread-pct` (after accounting for Kraken's taker fees), it simultaneously places a market buy on the cheaper pair and a market sell on the more expensive pair.
4. **Execute (Exit)**: It holds the position until the spread compresses back to equilibrium (the `exit-spread-pct`), at which point it reverses the trades to capture the profit.

## ✨ Features
- **100% Free Libraries**: Built entirely on free, open-source libraries (`ccxt`, `websockets`, `asyncio`). No paid enterprise licenses needed.
- **US-Legal**: Uses only spot trading on Kraken, fully compliant with US regulations for retail traders.
- **High-Frequency Websockets**: Uses Kraken's WebSocket v2 API for millisecond-latency order book updates.
- **Simultaneous Execution**: Wraps `ccxt` REST API calls in `asyncio.gather` threads to execute both legs of the arbitrage simultaneously.
- **Safety First**: Defaults to `--dry-run` mode. Extensive balance and fill-price validation.
- **Dockerized**: Ready to deploy on a $5/mo AWS Lightsail instance (runs on <512MB RAM).

---

## 🚀 Local Testing & Simulation (Dry-Run)

The easiest and safest way to test Zephyr is via Docker in **Dry-Run Mode**. The bot connects to Kraken's WebSockets, evaluates spreads, and logs "theoretical" trades without requiring an API key or risking real money.

### 1. Setup
```bash
git clone https://github.com/yourusername/zephyr.git
cd zephyr
cp .env.example .env
```

### 2. Run the Bot
To run the bot in the background:
```bash
docker compose up --build -d
```

### 3. Monitor Executed Trades
The bot generates a lot of data. To filter the logs and see **only** the executed trades (entries and exits) in real-time, run this in a new terminal window:
```bash
docker compose logs -f | grep "EXECUTED"
```

### 4. View the Trade Ledger (CSV)
Every completed trade (even dry-runs) is automatically saved to a persistent CSV file. You can open this file in Excel or Numbers to track your theoretical P&L:
```bash
# View the trades ledger
cat data/trades.csv
```
Columns include `total_pnl_usd`, `hold_time_sec`, and the exact entry/exit prices.

---

## ☁️ Amazon Lightsail Deployment (Live Trading)

Once you are satisfied with the dry-run simulations, you can deploy the bot to AWS Lightsail for 24/7 automated trading.

### 1. Get Kraken API Keys & Security
1. Go to [Kraken Pro Settings -> API](https://pro.kraken.com/app/settings/api).
2. Create a new API key.
3. **Permissions**: Grant "Funds" (Query) and "Orders & Trades" (Create, Cancel, Query). **DO NOT grant withdrawal permissions.**
4. **IP Whitelisting (Crucial)**: Once you set up your AWS server in Step 2, copy its public static IP address and paste it into the Kraken API "IP Whitelisting" field. This ensures that even if your keys are ever compromised, they are completely useless to anyone outside of your specific server.

### 2. Setup AWS Lightsail
1. Create a new **Amazon Lightsail** instance. A $5/mo Linux instance (OS Only -> Ubuntu) is perfect.
2. Connect to your instance via SSH.
3. Install Docker:
   ```bash
   sudo apt update
   sudo apt install docker.io docker-compose-v2 git -y
   ```
4. Clone your repository to the server:
   ```bash
   git clone <your-repo-url> zephyr
   cd zephyr
   ```

### 3. Configure the Bot
1. Open the `.env` file and add your Kraken keys (if you are ready for live trading):
   ```bash
   nano .env
   # Add:
   # KRAKEN_API_KEY=your_key
   # KRAKEN_API_SECRET=your_secret
   ```
2. Open `docker-compose.yml` (`nano docker-compose.yml`) to configure your strategy:
   - **For Dry-Run (Recommended First Step)**: Leave the `--dry-run` flag in the `command` section. This is highly recommended to verify your server's connection to Kraken and monitor theoretical trades without risking funds.
   - **For Live Trading**: **Remove the `--dry-run` flag** from the `command` section. You can also adjust your `--trade-amount-usd` and `--min-spread-pct` here.

### 4. Run in the Background
Start the bot in "detached" mode so it keeps running after you close your SSH connection:
```bash
docker compose up --build -d
```

### 5. Managing Your Bot
The configuration is already optimized with memory limits (512MB) and log rotation (max 150MB) to ensure it never crashes your Lightsail server.
- **View Live Trades**: `docker compose logs -f | grep "EXECUTED"`
- **View Trade History**: `cat data/trades.csv`
- **Stop the Bot**: `docker compose down`

---

## ⚙️ Configuration Flags

You can customize the bot's behavior in the `docker-compose.yml` `command` section:

| Flag | Default | Description |
|------|---------|-------------|
| `--dry-run` | `False` | Monitor opportunities and log them without placing real orders. |
| `--ignore-fees` | `False` | Pretend Kraken fees are 0.00% (useful for testing small spreads). |
| `--pairs` | `BTC/USD:BTC/USDT...` | Comma-separated list of spread pairs. Format: `PairA:PairB`. |
| `--trade-amount-usd`| `100.0` | USD notional amount to trade per leg. |
| `--min-spread-pct` | `0.5` | Minimum spread percentage (after fees) required to enter a trade. |
| `--max-positions` | `1` | Max concurrent open arbitrage positions per spread pair. |
| `--log-level` | `INFO` | Verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

## 🧪 Running Tests

To run the unit tests locally (requires Python 3.12+):

```bash
# Set up virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install pytest

# Run tests
python -m pytest tests/ -v
```
