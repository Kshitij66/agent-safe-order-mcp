# Agent-Safe Orders MCP Server

An [MCP](https://modelcontextprotocol.io) server that lets LLM agents (Claude Desktop, Cursor) work with a real
order and inventory database, without letting them break it.

Agents can **read** freely. Every **write** (cancel an order, issue a refund, adjust stock) becomes a
*pending action* that a human approves or rejects in a dashboard. Nothing changes until a human says yes.

![tests](https://img.shields.io/badge/tests-57%20passing-brightgreen) ![python](https://img.shields.io/badge/python-3.11%2B-blue) ![postgres](https://img.shields.io/badge/PostgreSQL-16-336791)

## Why

Once an agent has write access to production data, a bad prompt or a confused model can do real damage
("clean up all the old orders"). This project treats the agent as an untrusted client and puts it behind
the same controls you'd use for any external caller:

| Risk | Control |
|---|---|
| Agent makes a destructive change on its own | **Human-in-the-loop approval** for every write; actions expire after 30 min |
| Agent retries and refunds twice | **Idempotency keys** (derived from tool + params, or supplied) with a `UNIQUE` constraint |
| Two admins approve the same action at once | **Atomic compare-and-set claim** (`UPDATE ... WHERE status='pending'`) so it executes exactly once |
| Order changes between request and approval | Business rules **re-checked inside the write** (conditional `UPDATE`), so a stale action fails safely |
| Agent loops and spams requests | **Token-bucket rate limiting**, separate budgets for reads and writes |
| Agent has more access than it needs | **Permission scopes** (`orders:read`, `orders:write`, ...); read-only by default |
| "Delete everything" | **No bulk, delete or raw-SQL tools exist**; one record per call, capped list sizes, capped stock deltas |
| Invalid money / stock values | Integer cents, `CHECK` constraints in the DB (stock ≥ 0, refunds ≤ total) |
| Nobody knows what the agent did | **Append-only audit log** of every tool call (including denials) and every human decision |
| Internal errors leak to the model | Errors are mapped to safe messages; no stack traces or SQL reach the agent |

## Architecture

```mermaid
flowchart LR
    A[LLM agent<br/>Claude Desktop / Cursor] -- MCP over stdio --> M[MCP server<br/>8 tools]
    M --> G{ToolGateway<br/>scope → rate limit → validate → audit}
    G -- reads --> DB[(PostgreSQL)]
    G -- writes --> P[pending_actions]
    H[Human admin] -- dashboard / REST<br/>X-Admin-Token --> API[Approval API<br/>FastAPI]
    API -- approve: one transaction<br/>claim → execute → record --> DB
    P --- DB
```

```
src/order_mcp/
  mcp_server.py   MCP tool definitions (thin wrappers)
  gateway.py      the choke point: scopes, rate limits, validation, auditing
  services.py     business rules + atomic conditional writes
  approvals.py    pending → executed | failed | rejected | expired
  guard.py        token bucket + scope checks
  approval_api.py FastAPI dashboard and REST API for humans
  db.py           schema (SQLAlchemy Core), works on SQLite and Postgres
  audit.py        append-only audit log
  seed.py         demo data
```

### Action lifecycle

```
agent: request_refund(order 3, 899900, "arrived broken")
          │  validated, de-duplicated, stored
          ▼
      [pending] ──human rejects──► [rejected]
          │  └──── 30 min ──────► [expired]
     human approves
          ▼
   one DB transaction: claim row → re-check rules → write → store result
          ├─► [executed]
          └─► [failed]   (e.g. order already shipped; nothing written)
```

## MCP tools

| Tool | Scope | Effect |
|---|---|---|
| `get_order`, `search_orders` | `orders:read` | read |
| `list_products`, `check_stock` | `inventory:read` | read |
| `get_action_status` | `orders:read` | read |
| `request_order_cancellation` | `orders:write` | queues approval |
| `request_refund` | `orders:write` | queues approval |
| `request_stock_adjustment` | `inventory:write` | queues approval |

Write tools carry MCP `destructive_hint` annotations so clients can warn the user too.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Option A: SQLite, no setup
export DATABASE_URL=sqlite:///./orders.db ADMIN_TOKEN=change-me
order-seed
order-approvals            # dashboard at http://127.0.0.1:8000

# Option B: Postgres + dashboard in Docker
docker compose up --build  # dashboard at http://localhost:8000
```

Connect Claude Desktop: copy `claude_desktop_config.example.json` into your Claude Desktop config, fix the
path, restart, and ask things like *"Which orders for rahul@example.com have shipped?"* or
*"Refund order 3, the headphones arrived broken."* Then approve the request in the dashboard.

## Tests

```bash
pytest -q                                   # 54 tests on SQLite (3 concurrency tests skipped)
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/orders_test pytest -q   # all 57
```

- `test_agent_safety.py`: 29 adversarial cases: mass-cancel attempts, over-refunds, negative refunds,
  SQL injection in arguments, missing reasons, scope bypass, retries, error leakage
- `test_concurrency.py`: real races on Postgres: 10 simultaneous approvals of one action (executes once),
  10 parallel refunds against one order (never exceeds the total), 10 identical requests (one queued)
- `test_approvals.py`, `test_services.py`, `test_guard.py`, `test_approval_api.py`: unit + API tests

CI runs both suites on every push (GitHub Actions with a Postgres service container).

## Design notes

- **Why not let the agent call the DB directly?** An agent is a non-deterministic client. The gateway
  makes its blast radius small and every action attributable.
- **Why re-validate at approval time?** Minutes can pass between request and approval. Checking only at
  request time is a time-of-check/time-of-use bug.
- **Why idempotency keys from params?** LLM clients retry on timeouts. Hashing `tool + params` means a
  retried request maps to the existing action instead of creating a second one.
- **Why integer cents?** `0.1 + 0.2 != 0.3` in floating point.
- **Why stdout is never printed to:** with stdio transport, stdout *is* the protocol channel.

## License

MIT
