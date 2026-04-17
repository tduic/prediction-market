"""
Configuration management for the prediction market trading system.
Loads all settings from environment variables with sensible defaults.

This is the single source of truth for all configuration.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from core.secrets import get_secret


@dataclass
class PlatformCredentials:
    """Platform-specific API credentials."""

    polymarket_private_key: str = field(
        default_factory=lambda: get_secret("POLYMARKET_PRIVATE_KEY", "") or ""
    )
    polymarket_wallet_address: str = field(
        default_factory=lambda: get_secret("POLYMARKET_WALLET_ADDRESS", "") or ""
    )
    kalshi_api_key: str = field(
        default_factory=lambda: get_secret("KALSHI_API_KEY", "") or ""
    )
    kalshi_rsa_key_path: str = field(
        default_factory=lambda: get_secret("KALSHI_RSA_KEY_PATH", "") or ""
    )
    kalshi_environment: str = field(
        default_factory=lambda: os.getenv("KALSHI_ENVIRONMENT", "prod")
    )
    kalshi_api_base: str = field(
        default_factory=lambda: os.getenv(
            "KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2"
        )
    )


@dataclass
class DatabaseConfig:
    """Database connection settings."""

    db_path: str = field(
        default_factory=lambda: os.getenv("DB_PATH", "prediction_market.db")
    )
    migrations_dir: str = field(
        default_factory=lambda: os.getenv("MIGRATIONS_DIR", "core/storage/migrations")
    )

    def __post_init__(self):
        """Ensure database directory exists."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)


@dataclass
class IngestorConfig:
    """Market data ingestor settings."""

    poll_interval_polymarket_s: int = field(
        default_factory=lambda: int(os.getenv("POLL_INTERVAL_POLYMARKET_S", "30"))
    )
    poll_interval_kalshi_s: int = field(
        default_factory=lambda: int(os.getenv("POLL_INTERVAL_KALSHI_S", "30"))
    )
    poll_interval_external_s: int = field(
        default_factory=lambda: int(os.getenv("POLL_INTERVAL_EXTERNAL_S", "300"))
    )
    max_markets_per_poll: int = field(
        default_factory=lambda: int(os.getenv("MAX_MARKETS_PER_POLL", "500"))
    )


@dataclass
class RiskControlConfig:
    """Risk management settings.

    All limits are expressed as percentages of portfolio value so they
    scale automatically as the account grows or shrinks.

    Portfolio value is computed as:
        starting_capital + realized_pnl - total_fees
    """

    starting_capital: float = field(
        default_factory=lambda: float(os.getenv("STARTING_CAPITAL", "10000"))
    )
    max_position_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_POSITION_PCT", "0.05"))
    )
    max_daily_loss_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_DAILY_LOSS_PCT", "0.02"))
    )
    max_portfolio_exposure_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_PORTFOLIO_EXPOSURE_PCT", "0.20"))
    )
    kelly_fraction: float = field(
        default_factory=lambda: float(os.getenv("KELLY_FRACTION", "0.25"))
    )
    duplicate_signal_window_s: int = field(
        default_factory=lambda: int(os.getenv("DUPLICATE_SIGNAL_WINDOW_S", "300"))
    )
    min_edge: float = field(
        default_factory=lambda: float(os.getenv("MIN_EDGE_TO_TRADE", "0.02"))
    )
    consecutive_failure_limit: int = field(
        default_factory=lambda: int(os.getenv("CONSECUTIVE_FAILURE_LIMIT", "5"))
    )
    arb_cooldown_s: float = field(
        default_factory=lambda: float(os.getenv("ARB_COOLDOWN_S", "60"))
    )
    arb_rearm_hysteresis: float = field(
        default_factory=lambda: float(os.getenv("ARB_REARM_HYSTERESIS", "0.005"))
    )
    # Maximum age (seconds) of the cached price on either side of a pair
    # before the arb engine refuses to fire. A price that hasn't been
    # confirmed by a websocket tick within this window is assumed to be
    # stale — firing on it would set a limit that the real market has
    # already drifted past, producing the "Market price X below limit Y"
    # rejections we saw on Kalshi. Seed prices (from the matcher) get a
    # fresh tick-time stamp at engine startup, so the guard only kicks
    # in once the seed window has elapsed without a real WS confirm.
    max_price_age_s: float = field(
        default_factory=lambda: float(os.getenv("MAX_PRICE_AGE_S", "10"))
    )
    slippage_bps: float = field(
        default_factory=lambda: float(os.getenv("SLIPPAGE_BPS", "10"))
    )
    strategy_holding_period_s: int = field(
        default_factory=lambda: int(os.getenv("STRATEGY_HOLDING_PERIOD_S", "300"))
    )
    strategy_replay_cooldown_s: int = field(
        default_factory=lambda: int(os.getenv("STRATEGY_REPLAY_COOLDOWN_S", "300"))
    )
    strategy_replay_min_move: float = field(
        default_factory=lambda: float(os.getenv("STRATEGY_REPLAY_MIN_MOVE", "0.01"))
    )
    strategy_p2_enabled: bool = field(
        default_factory=lambda: os.getenv("STRATEGY_P2_ENABLED", "true").lower()
        == "true"
    )
    strategy_p3_enabled: bool = field(
        default_factory=lambda: os.getenv("STRATEGY_P3_ENABLED", "true").lower()
        == "true"
    )
    strategy_p4_enabled: bool = field(
        default_factory=lambda: os.getenv("STRATEGY_P4_ENABLED", "true").lower()
        == "true"
    )
    strategy_p5_enabled: bool = field(
        default_factory=lambda: os.getenv("STRATEGY_P5_ENABLED", "true").lower()
        == "true"
    )
    strategy_killswitch_window_s: int = field(
        default_factory=lambda: int(os.getenv("STRATEGY_KILLSWITCH_WINDOW_S", "604800"))
    )
    strategy_killswitch_min_trades: int = field(
        default_factory=lambda: int(os.getenv("STRATEGY_KILLSWITCH_MIN_TRADES", "5"))
    )


@dataclass
class ExecutionConfig:
    """Order execution and settlement settings."""

    execution_mode: str = field(
        default_factory=lambda: os.getenv("EXECUTION_MODE", "paper")
    )
    max_order_retries: int = field(
        default_factory=lambda: int(os.getenv("MAX_ORDER_RETRIES", "3"))
    )
    retry_backoff_base_s: float = field(
        default_factory=lambda: float(os.getenv("RETRY_BACKOFF_BASE_S", "1"))
    )
    partial_fill_cancel_window_s: int = field(
        default_factory=lambda: int(os.getenv("PARTIAL_FILL_CANCEL_WINDOW_S", "5"))
    )


@dataclass
class ObservabilityConfig:
    """Logging and monitoring settings."""

    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    log_format: str = field(default_factory=lambda: os.getenv("LOG_FORMAT", "text"))
    pnl_snapshot_interval_s: int = field(
        default_factory=lambda: int(os.getenv("PNL_SNAPSHOT_INTERVAL_S", "3600"))
    )


@dataclass
class Config:
    """Main configuration class combining all sub-configurations."""

    platform_credentials: PlatformCredentials = field(
        default_factory=PlatformCredentials
    )
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    ingestor: IngestorConfig = field(default_factory=IngestorConfig)
    risk_controls: RiskControlConfig = field(default_factory=RiskControlConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)

    def __post_init__(self):
        """Validate configuration after initialization."""
        self._validate()

    def _validate(self):
        """Validate all configuration constraints."""
        # Execution mode validation
        if self.execution.execution_mode not in ("live", "paper", "shadow"):
            raise ValueError(
                f"EXECUTION_MODE must be 'live', 'paper', or 'shadow', "
                f"got {self.execution.execution_mode}"
            )

        # Credentials validation — only required for live mode
        if self.execution.execution_mode == "live":
            if not all(
                [
                    self.platform_credentials.polymarket_private_key,
                    self.platform_credentials.polymarket_wallet_address,
                    self.platform_credentials.kalshi_api_key,
                    self.platform_credentials.kalshi_rsa_key_path,
                ]
            ):
                raise ValueError(
                    "All platform credentials required when EXECUTION_MODE=live"
                )

        # Kelly fraction validation
        if not (0 < self.risk_controls.kelly_fraction <= 0.5):
            raise ValueError(
                f"KELLY_FRACTION must be > 0 and <= 0.5, "
                f"got {self.risk_controls.kelly_fraction}"
            )

        # Position size validation
        if not (0 < self.risk_controls.max_position_pct <= 1):
            raise ValueError(
                f"MAX_POSITION_PCT must be > 0 and <= 1, "
                f"got {self.risk_controls.max_position_pct}"
            )

        # Daily loss validation
        if not (0 < self.risk_controls.max_daily_loss_pct <= 1):
            raise ValueError(
                f"MAX_DAILY_LOSS_PCT must be > 0 and <= 1, "
                f"got {self.risk_controls.max_daily_loss_pct}"
            )

        # Starting capital validation
        if self.risk_controls.starting_capital <= 0:
            raise ValueError(
                f"STARTING_CAPITAL must be > 0, "
                f"got {self.risk_controls.starting_capital}"
            )


def load_config() -> Config:
    """Load configuration from environment variables."""
    return Config()


# Global config instance
_config: Config | None = None


def get_config() -> Config:
    """Get or create the global configuration instance."""
    global _config
    if _config is None:
        _config = load_config()
    return _config
