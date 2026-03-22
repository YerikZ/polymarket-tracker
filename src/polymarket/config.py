import os
import yaml
from pathlib import Path

_DEFAULTS = {
    "top_n": 20,
    "poll_interval": 300,
    "min_position_usdc": 50.0,
    "request_delay": 0.5,
    "max_retries": 3,
    "leaderboard_ttl": 3600,
    "data_dir": "./data",   # kept for db-migrate command; not used by Storage itself
    "log_level": "INFO",
    "polygon_wss": "",
    "wallet_refresh_interval": 600,
    "database_url": "postgresql://polymarket:polymarket@localhost:5433/polymarket",
}

_ENV_MAP = {
    "POLYMARKET_TOP_N":           ("top_n", int),
    "POLYMARKET_POLL_INTERVAL":   ("poll_interval", int),
    "POLYMARKET_MIN_SIZE":        ("min_position_usdc", float),
    "POLYMARKET_REQUEST_DELAY":   ("request_delay", float),
    "POLYMARKET_LOG_LEVEL":       ("log_level", str),
    "POLYMARKET_DATABASE_URL":    ("database_url", str),
}


def load(config_path: str | None = None) -> dict:
    cfg = dict(_DEFAULTS)

    # Find config.yaml relative to project root (two levels up from this file)
    if config_path is None:
        candidate = Path(__file__).parent.parent.parent / "config.yaml"
        if candidate.exists():
            config_path = str(candidate)

    if config_path and Path(config_path).exists():
        with open(config_path) as f:
            file_cfg = yaml.safe_load(f) or {}
        cfg.update(file_cfg)

    # Environment variable overrides (top-level keys)
    for env_key, (cfg_key, cast) in _ENV_MAP.items():
        val = os.environ.get(env_key)
        if val is not None:
            cfg[cfg_key] = cast(val)

    # WebSocket endpoint — env var takes priority
    if os.environ.get("POLYMARKET_POLYGON_WSS"):
        cfg["polygon_wss"] = os.environ["POLYMARKET_POLYGON_WSS"]

    # Secrets for copy-trading — env vars take priority over config file
    ct = cfg.setdefault("copy_trading", {})
    if os.environ.get("POLYMARKET_PRIVATE_KEY"):
        ct["private_key"] = os.environ["POLYMARKET_PRIVATE_KEY"]
    if os.environ.get("POLYMARKET_FUNDER"):
        ct["funder"] = os.environ["POLYMARKET_FUNDER"]

    return cfg
