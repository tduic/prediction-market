"""
Configuration management for the prediction market trading system.
Loads all settings from environment variables with sensible defaults.

This is the single source of truth for all configuration. Both the
core service and execution service use get_config() to read settings.
"""

from dataclasses import dataclass, field

import os
from pathlib import Path


@dataclass
class PlatformCredentials:
    """Platform-specific API credentials."""

    polymarket_private_key: str = field(
        default_factory=lambda: os.getenv("POLYMARKET_PRIVATE_KEY", "")
    )
    polymarket_wallet_address: str = field(
        default_factory=lambda: os.getenv("POLYMARKET_WALLET_ADDRESS", "")
    )
    kalshi_api_key: str = field(default_factory=lambda: os.getenv("KALSHI_API_KEY", ""))
    kalshi_rsa_key_path: str = field(
        default_factory=lambda: os.getenv("KALSHI_RSA_KEY_PATH", "")
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
class RedisConfig:
    """Redis connection settings."""

    redis_url: str = field(
        default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379")
    )
    signal_queue_name: str = field(
        default_factory=lambda: os.getenv("SIGNAL_QUEUE_NAME", "trading_signals")
    )
    signal_queue_timeout_s: int = field(
        default_factory=lambda: int(os.getenv("SIGNAL_QUEUE_TIMEOUT_S", "5"))
    )


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
class ConstraintEngineConfig:
    """Constraint engine and fee settings."""

    min_net_spread_single_platform: float = field(
        default_factory=lambda: float(
            os.getenv("MIN_NET_SPREAD_SINGLE_PLATFORM", "0.03")
        )
    )
    min_net_spread_cross_platform: float = field(
        default_factory=lambda: float(
            os.getenv("MIN_NET_SPREAD_CROSS_PLATFORM", "0.04")
        )
    )
    fee_rate_polymarket: float = field(
        default_factory=lambda: float(os.getenv("FEE_RATE_POLYMARKET", "0.02"))
    )
    fee_rate_kalshi: float = field(
        default_factory=lambda: float(os.getenv("FEE_RATE_KALSHI", "0.02"))
    )


@dataclass
class RiskControlConfig:
    """Risk management settings."""

    max_position_size_usd: float = field(
        default_factory=lambda: float(os.getenv("MAX_POSITION_SIZE_USD", "500"))
    )
    max_daily_loss_usd: float = field(
        default_factory=lambda: float(os.getenv("MAX_DAILY_LOSS_USD", "200"))
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


@dataclass
class ModelServiceConfig:
    """Model training and deployment settings."""

    model_refit_cron: str = field(
        default_factory=lambda: os.getenv("MODEL_REFIT_CRON", "0 2 * * *")
    )
    min_edge_to_signal: float = field(
        default_factory=lambda: float(os.getenv("MIN_EDGE_TO_SIGNAL", "0.05"))
    )
    min_training_samples: int = field(
        default_factory=lambda: int(os.getenv("MIN_TRAINING_SAMPLES", "30"))
    )


@dataclass
class MatchingConfig:
    """Market matching and verification settings."""

    embedding_model: str = field(
        default_factory=lambda: os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    )
    similarity_threshold: float = field(
        default_factory=lambda: float(os.getenv("SIMILARITY_THRESHOLD", "0.85"))
    )
    auto_trade_verified_only: bool = field(
        default_factory=lambda: os.getenv("AUTO_TRADE_VERIFIED_ONLY", "true").lower()
        == "true"
    )


@dataclass
class ExecutionConfig:
    """Order execution and settlement settings."""

    execution_mode: str = field(
        default_factory=lambda: os.getenv("EXECUTION_MODE", "mock")
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
    redis: RedisConfig = field(default_factory=RedisConfig)
    ingestor: IngestorConfig = field(default_factory=IngestorConfig)
    constraint_engine: ConstraintEngineConfig = field(
        default_factory=ConstraintEngineConfig
    )
    risk_controls: RiskControlConfig = field(default_factory=RiskControlConfig)
    model_service: ModelServiceConfig = field(default_factory=ModelServiceConfig)
    matching: MatchingConfig = field(default_factory=MatchingConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)

    def __post_init__(self):
        """Validate configuration after initialization."""
        self._validate()

    def _validate(self):
        """Validate all configuration constraints."""
        # Execution mode validation
        if self.execution.execution_mode not in ("live", "mock", "paper"):
            raise ValueError(
                f"EXECUTION_MODE must be 'live', 'mock', or 'paper', "
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
        if self.risk_controls.max_position_size_usd <= 0:
            raise ValueError(
                f"MAX_POSITION_SIZE_USD must be > 0, "
                f"got {self.risk_controls.max_position_size_usd}"
            )

        # Spread validation
        if (
            self.constraint_engine.min_net_spread_single_platform
            <= self.constraint_engine.fee_rate_polymarket
        ):
            raise ValueError(
                "MIN_NET_SPREAD_SINGLE_PLATFORM must be > FEE_RATE_POLYMARKET"
            )
        if self.constraint_engine.min_net_spread_cross_platform <= max(
            self.constraint_engine.fee_rate_polymarket,
            self.constraint_engine.fee_rate_kalshi,
        ):
            raise ValueError(
                "MIN_NET_SPREAD_CROSS_PLATFORM must be > "
                "max(FEE_RATE_POLYMARKET, FEE_RATE_KALSHI)"
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
