"""Runtime configuration, read from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _split(value: str) -> frozenset[str]:
    return frozenset(s.strip() for s in value.split(",") if s.strip())


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./orders.db"
    # Least privilege by default: an agent only gets read scopes unless configured otherwise.
    scopes: frozenset[str] = field(default_factory=lambda: frozenset({"orders:read", "inventory:read"}))
    admin_token: str = ""
    action_ttl_minutes: int = 30
    read_rate_per_minute: int = 60
    write_rate_per_minute: int = 10
    max_stock_delta: int = 1000

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.getenv("DATABASE_URL", cls.database_url),
            scopes=_split(os.getenv("MCP_SCOPES", "orders:read,inventory:read")),
            admin_token=os.getenv("ADMIN_TOKEN", ""),
            action_ttl_minutes=int(os.getenv("ACTION_TTL_MINUTES", "30")),
            read_rate_per_minute=int(os.getenv("READ_RATE_PER_MINUTE", "60")),
            write_rate_per_minute=int(os.getenv("WRITE_RATE_PER_MINUTE", "10")),
            max_stock_delta=int(os.getenv("MAX_STOCK_DELTA", "1000")),
        )
