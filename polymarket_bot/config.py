import os
from dotenv import load_dotenv

load_dotenv()

# Wallet / chain (only required for live trading, not paper trading)
PRIVATE_KEY: str = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS: str = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
CHAIN_ID: int = 137  # Polygon mainnet
SIGNATURE_TYPE: int = 0  # 0 = EOA/MetaMask; 1 = Magic/email proxy wallet

# Polymarket endpoints
GAMMA_API_BASE: str = "https://gamma-api.polymarket.com"
CLOB_API_BASE: str = "https://clob.polymarket.com"

# Anthropic
ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL: str = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS: int = 512

# Token cost in USD per 1M tokens (claude-sonnet-4-6)
CLAUDE_INPUT_COST_PER_M: float = 3.00
CLAUDE_OUTPUT_COST_PER_M: float = 15.00

# Scanning
SCAN_INTERVAL_SECONDS: int = 600       # 10-minute cycle
MAX_MARKETS_PER_SCAN: int = 1000
MIN_VOLUME_24H: float = 500.0          # ignore illiquid markets
MIN_LIQUIDITY: float = 1000.0
MIN_BEST_BID: float = 0.03             # skip near-certainties
MAX_BEST_ASK: float = 0.97

# Strategy
MISPRICING_THRESHOLD: float = 0.03    # 3% minimum edge (free heuristic; raise to 8% for live Claude mode)
HALF_KELLY_FRACTION: float = 0.5
MAX_BET_FRACTION: float = 0.06        # hard cap: 6% of bankroll per trade
MIN_BET_USDC: float = 0.10            # paper trading min; CLOB live minimum is $15

# Kill switch
MIN_USDC_BALANCE: float = 0.0

# Logging
LOG_FILE: str = "polymarket_bot.log"
LOG_MAX_BYTES: int = 10 * 1024 * 1024
LOG_BACKUP_COUNT: int = 5
