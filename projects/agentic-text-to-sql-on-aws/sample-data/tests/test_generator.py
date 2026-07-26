"""생성기 결정성 및 분포 테스트."""

from __future__ import annotations

import datetime as dt

from sample_data import generator


def test_determinism_same_seed_same_data():
    a = generator.generate(n_customers=50, n_products=20, n_orders=200)
    b = generator.generate(n_customers=50, n_products=20, n_orders=200)
    assert a.customers == b.customers
    assert a.products == b.products
    assert a.orders == b.orders
    assert a.order_items == b.order_items


def test_different_seed_differs():
    a = generator.generate(seed=42, n_customers=50, n_products=20, n_orders=200)
    b = generator.generate(seed=7, n_customers=50, n_products=20, n_orders=200)
    assert a.customers != b.customers


def test_row_counts_match_requested():
    ds = generator.generate(n_customers=100, n_products=30, n_orders=500)
    counts = ds.row_counts()
    assert counts["customers"] == 100
    assert counts["products"] == 30
    assert counts["orders"] == 500
    assert counts["categories"] == len(generator.CATEGORY_DEFS)
    # 각 주문은 최소 1개 라인.
    assert counts["order_items"] >= counts["orders"]


def test_foreign_keys_valid():
    ds = generator.generate(n_customers=50, n_products=20, n_orders=200)
    customer_ids = {c["id"] for c in ds.customers}
    product_ids = {p["id"] for p in ds.products}
    category_ids = {c["id"] for c in ds.categories}
    order_ids = {o["id"] for o in ds.orders}

    assert all(p["category_id"] in category_ids for p in ds.products)
    assert all(o["customer_id"] in customer_ids for o in ds.orders)
    assert all(i["order_id"] in order_ids for i in ds.order_items)
    assert all(i["product_id"] in product_ids for i in ds.order_items)


def test_total_amount_matches_line_items():
    ds = generator.generate(n_customers=30, n_products=15, n_orders=100)
    items_by_order: dict[int, int] = {}
    for item in ds.order_items:
        line = int(float(item["unit_price"])) * item["quantity"]
        items_by_order[item["order_id"]] = items_by_order.get(item["order_id"], 0) + line
    for order in ds.orders:
        assert int(float(order["total_amount"])) == items_by_order[order["id"]]


def test_orders_within_24_months():
    ds = generator.generate(n_customers=30, n_products=15, n_orders=300)
    earliest = generator.NOW - dt.timedelta(days=731)
    for order in ds.orders:
        ordered = dt.datetime.fromisoformat(order["ordered_at"])
        assert earliest <= ordered <= generator.NOW


def test_last_login_after_created():
    ds = generator.generate(n_customers=200, n_products=10, n_orders=50)
    for c in ds.customers:
        created = dt.datetime.fromisoformat(c["created_at"])
        last_login = dt.datetime.fromisoformat(c["last_login_at"])
        assert last_login >= created


def test_region_distribution_covers_regions():
    ds = generator.generate(n_customers=500, n_products=10, n_orders=50)
    regions = {c["region"] for c in ds.customers}
    # 수도권 집중이지만 다수 지역이 등장해야 함.
    assert "서울" in regions
    assert len(regions) >= 5


def test_active_and_inactive_customers_exist():
    ds = generator.generate(n_customers=500, n_products=10, n_orders=50)
    cutoff = generator.NOW - dt.timedelta(days=90)
    active = [
        c
        for c in ds.customers
        if dt.datetime.fromisoformat(c["last_login_at"]) >= cutoff
    ]
    inactive = [
        c
        for c in ds.customers
        if dt.datetime.fromisoformat(c["last_login_at"]) < cutoff
    ]
    # '최근 활성 고객' 질의가 의미를 갖도록 양쪽 다 존재.
    assert len(active) > 0
    assert len(inactive) > 0
