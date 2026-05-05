# ⚡ Zephyr — Market Making Bot

Zephyr is a high-performance, Dockerized trading bot that acts as an automated **market maker** on the **Kraken** exchange.

Because futures and margin trading are heavily restricted for US residents, this bot is designed to be **100% US-legal**. It uses a spot-only strategy, placing limit orders around the mid-price of altcoins to capture the bid-ask spread.

It uses open-source libraries (`ccxt` + `websockets`) to stream data via Kraken's WebSocket API v2 and execute dual-leg spot limit orders simultaneously.

## 🧠 Strategy: Inventory-Skewed Market Making

Instead of trading spot against futures (which requires margin), Zephyr provides liquidity on a single spot pair (e.g., `SOL/USD`).

It uses an advanced algorithm inspired by **Avellaneda-Stoikov**, continuously tracking the optimal "fair value" (reservation price) and adjusting quotes based on your current inventory.

**How it works:**
1. **Monitor**: The bot streams live order book data and account balances.
2. **Evaluate (Fair Value)**: It calculates the mid-price and calculates your "inventory delta" (how much your base vs quote balance deviates from a target 50/50 ratio).
3. **Evaluate (Skew)**: It skews the reservation price down if you hold too much inventory (attracting buyers) and up if you hold too little (attracting sellers), actively managing risk.
4. **Execute (Quote)**: It places limit Buy and Sell orders around the reservation price based on your configured `--maker-base-spread-pct`.
5. **Execute (Refresh)**: If the market moves away from your limit orders by more than your `--order-refresh-tolerance-pct`, it automatically cancels the stale orders and quotes new optimal prices.

## ✨ Features
- **Advanced Pricing Logic**: Dynamic, inventory-aware quoting that minimizes directional risk.
- **100% Free Libraries**: Built entirely on free, open-source libraries (`ccxt`, `websockets`, `asyncio`). No paid enterprise licenses needed.
- **US-Legal**: Uses only spot trading on Kraken, fully compliant with US regulations for retail traders.
- **High-Frequency Websockets**: Uses Kraken's WebSocket v2 API for millisecond-latency order book updates.
- **Safety First**: Defaults to `--dry-run` mode. Automatic cancellation of open orders on shutdown.
- **Dockerized**: Ready to deploy on a $5/mo AWS Lightsail instance (runs on <512MB RAM).

---

## 🚀 Local Testing & Simulation (Dry-Run)

The easiest and safest way to test Zephyr is via Docker in **Dry-Run Mode**. The bot connects to Kraken's WebSockets, evaluates optimal limit prices, and tracks "theoretical" orders without requiring an API key or risking real money.

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

### 3. Monitor Quotes
The bot generates log data for its quoting logic. To see the bot calculating its skew and placing simulated limit orders:
```bash
docker compose logs -f | grep "Updating quotes"
```

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
   - **For Dry-Run (Recommended First Step)**: Leave the `--dry-run` flag in the `command` section. This is highly recommended to verify your server's connection to Kraken.
   - **For Live Trading**: **Remove the `--dry-run` flag** from the `command` section.

### 4. Run in the Background
Start the bot in "detached" mode so it keeps running after you close your SSH connection:
```bash
docker compose up --build -d
```

### 5. Managing Your Bot
The configuration is already optimized with memory limits (512MB) and log rotation (max 150MB) to ensure it never crashes your Lightsail server.
- **View Live Logs**: `docker compose logs -f`
- **Stop the Bot**: `docker compose down`

---

## ⚙️ Configuration Flags

You can customize the bot's behavior in the `docker-compose.yml` `command` section:

| Flag | Default | Description |
|------|---------|-------------|
| `--dry-run` | `False` | Monitor opportunities and log them without placing real orders. |
| `--ignore-fees` | `False` | Pretend Kraken fees are 0.00% (useful for testing). |
| `--symbols` | `SOL/USD,DOGE/USD` | Comma-separated list of symbols to market make on. |
| `--trade-amount-usd`| `100.0` | USD notional amount per limit order. |
| `--maker-base-spread-pct` | `0.5` | Base target spread (ask - bid) as a %. |
| `--inventory-risk-aversion` | `0.1` | How aggressively to skew quotes based on inventory imbalance. |
| `--order-refresh-tolerance-pct`| `0.05`| Re-quote if optimal price moves more than this % away from current order. |
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
