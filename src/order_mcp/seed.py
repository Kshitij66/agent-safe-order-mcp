"""Create tables and load demo data (only if the database is empty)."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, insert, select
from sqlalchemy.engine import Engine

from .config import Settings
from .db import init_db, make_engine, orders, products, utcnow

PRODUCTS = [
    ("KB-101", "Mechanical Keyboard", 349900, 40),
    ("MS-202", "Wireless Mouse", 129900, 120),
    ("HD-303", "Noise-Cancelling Headphones", 899900, 15),
    ("MN-404", "27-inch Monitor", 1899900, 8),
    ("CB-505", "USB-C Cable", 49900, 300),
]

# (email, sku, qty, status, refunded_cents, days_ago)
ORDERS = [
    ("asha@example.com", "KB-101", 1, "placed", 0, 1),
    ("asha@example.com", "CB-505", 3, "delivered", 0, 12),
    ("rahul@example.com", "HD-303", 1, "shipped", 0, 3),
    ("rahul@example.com", "MS-202", 2, "delivered", 129900, 20),
    ("meera@example.com", "MN-404", 1, "placed", 0, 0),
    ("meera@example.com", "CB-505", 1, "cancelled", 0, 30),
    ("vikram@example.com", "KB-101", 2, "delivered", 0, 7),
    ("vikram@example.com", "HD-303", 1, "refunded", 899900, 45),
]


def seed(engine: Engine) -> bool:
    init_db(engine)
    with engine.begin() as conn:
        if conn.execute(select(func.count()).select_from(products)).scalar():
            return False
        ids = {}
        for sku, name, price, stock in PRODUCTS:
            ids[sku] = (conn.execute(insert(products).values(sku=sku, name=name, price_cents=price, stock=stock))
                        .inserted_primary_key[0], price)
        now = utcnow()
        for email, sku, qty, status, refunded, days in ORDERS:
            pid, price = ids[sku]
            conn.execute(insert(orders).values(
                customer_email=email, product_id=pid, quantity=qty, total_cents=price * qty,
                refunded_cents=refunded, status=status, created_at=now - timedelta(days=days),
            ))
    return True


def main() -> None:
    settings = Settings.from_env()
    created = seed(make_engine(settings.database_url))
    print("Seeded demo data." if created else "Database already has data; skipped.")


if __name__ == "__main__":
    main()
