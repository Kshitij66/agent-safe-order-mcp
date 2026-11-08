import os

import pytest

from order_mcp.config import Settings
from order_mcp.db import make_engine, metadata
from order_mcp.gateway import ToolGateway
from order_mcp.guard import Guard
from order_mcp.seed import seed

ALL_SCOPES = frozenset({"orders:read", "inventory:read", "orders:write", "inventory:write"})


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


# Default: a fresh SQLite file per test. Set TEST_DATABASE_URL to run against Postgres, e.g.
#   TEST_DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/orders_test pytest
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(TEST_DATABASE_URL or f"sqlite:///{tmp_path / 'test.db'}")
    metadata.drop_all(eng)
    seed(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def settings():
    return Settings(database_url="unused", scopes=ALL_SCOPES, admin_token="secret-token",
                    action_ttl_minutes=30, read_rate_per_minute=1000, write_rate_per_minute=1000)


@pytest.fixture
def gateway(engine, settings):
    return ToolGateway(engine, settings, actor="agent:test")


@pytest.fixture
def make_gateway(engine, settings):
    def _make(scopes=ALL_SCOPES, read=1000, write=1000, clock=None):
        guard = Guard(frozenset(scopes), read, write, clock or FakeClock())
        return ToolGateway(engine, settings, guard=guard, actor="agent:test")
    return _make
