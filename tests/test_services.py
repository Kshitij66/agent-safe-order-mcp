import pytest

from order_mcp import services
from order_mcp.services import DomainError, InvalidState, NotFound


def stock(engine, sku):
    with engine.connect() as c:
        return services.get_product(c, sku)["stock"]


def test_get_order_and_not_found(engine):
    with engine.connect() as c:
        o = services.get_order(c, 1)
        assert o["sku"] == "KB-101" and o["status"] == "placed"
        with pytest.raises(NotFound):
            services.get_order(c, 999)


def test_search_orders_filters_and_rejects_unknown_status(engine):
    with engine.connect() as c:
        assert {o["customer_email"] for o in services.search_orders(c, "ASHA@example.com ")} == {"asha@example.com"}
        assert all(o["status"] == "delivered" for o in services.search_orders(c, status="delivered"))
        with pytest.raises(DomainError):
            services.search_orders(c, status="deleted")


def test_cancel_restocks_atomically(engine):
    before = stock(engine, "KB-101")
    with engine.begin() as c:
        r = services.execute_cancel_order(c, 1)
    assert r["status"] == "cancelled"
    assert stock(engine, "KB-101") == before + 1


def test_cancel_twice_fails_and_does_not_double_restock(engine):
    with engine.begin() as c:
        services.execute_cancel_order(c, 1)
    after_first = stock(engine, "KB-101")
    with pytest.raises(InvalidState), engine.begin() as c:
        services.execute_cancel_order(c, 1)
    assert stock(engine, "KB-101") == after_first


def test_cannot_cancel_shipped_order(engine):
    with engine.connect() as c, pytest.raises(InvalidState):
        services.validate_cancel_order(c, 3)


def test_partial_then_full_refund(engine):
    with engine.begin() as c:
        r = services.execute_refund(c, 7, 100000)
    assert r["status"] == "delivered" and r["refunded_total_cents"] == 100000
    with engine.begin() as c:
        r = services.execute_refund(c, 7, 699800 - 100000)
    assert r["status"] == "refunded"


def test_refund_cannot_exceed_total(engine):
    with engine.connect() as c, pytest.raises(DomainError):
        services.validate_refund(c, 4, 129901)  # only 129900 left on order 4
    with pytest.raises(InvalidState), engine.begin() as c:
        services.execute_refund(c, 4, 129901)


def test_refund_rejects_non_positive_and_wrong_state(engine):
    with engine.connect() as c:
        with pytest.raises(DomainError):
            services.validate_refund(c, 2, 0)
        with pytest.raises(InvalidState):
            services.validate_refund(c, 1, 100)  # placed, not yet shipped


def test_stock_adjustment_never_goes_negative(engine):
    with engine.begin() as c:
        assert services.execute_stock_adjustment(c, "MN-404", -8)["stock"] == 0
    with pytest.raises(InvalidState), engine.begin() as c:
        services.execute_stock_adjustment(c, "MN-404", -1)
    with engine.connect() as c, pytest.raises(DomainError):
        services.validate_stock_adjustment(c, "MN-404", 5000, max_delta=1000)
