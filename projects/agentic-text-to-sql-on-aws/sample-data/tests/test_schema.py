"""스키마 DDL 문법 및 구조 테스트 (sqlglot parse)."""

from __future__ import annotations

import sqlglot

from sample_data import schema


def test_all_create_statements_parse():
    for table in schema.TABLES:
        parsed = sqlglot.parse_one(table.create_ddl(), read="postgres")
        assert parsed is not None


def test_full_ddl_parses_as_multiple_statements():
    ddl = schema.build_schema_ddl()
    statements = sqlglot.parse(ddl, read="postgres")
    # CREATE + COMMENT + INDEX 문들이 모두 파싱되어야 함.
    assert len(statements) > len(schema.TABLES)
    assert all(s is not None for s in statements)


def test_iter_ddl_statements_each_parse():
    for stmt in schema.iter_ddl_statements():
        parsed = sqlglot.parse_one(stmt, read="postgres")
        assert parsed is not None


def test_every_column_has_comment():
    for table in schema.TABLES:
        assert table.comment
        for column in table.columns:
            assert column.comment, f"{table.name}.{column.name} 코멘트 누락"


def test_each_table_has_primary_key():
    for table in schema.TABLES:
        pks = [c for c in table.columns if c.primary_key]
        assert len(pks) == 1, f"{table.name} PK 정의 오류"


def test_foreign_key_references_exist():
    table_names = {t.name for t in schema.TABLES}
    for table in schema.TABLES:
        for column in table.columns:
            if column.references:
                ref_table = column.references.split("(")[0]
                assert ref_table in table_names


def test_indexes_present():
    # 인덱스가 최소 몇 개 정의되어야 함.
    total = sum(len(t.indexes) for t in schema.TABLES)
    assert total >= 5


def test_comment_escapes_single_quotes():
    literal = schema._sql_str("it's a test")
    assert literal == "'it''s a test'"


def test_tables_in_fk_dependency_order():
    # categories/customers 가 products/orders 보다 먼저 와야 함(FK 순서).
    order = [t.name for t in schema.TABLES]
    assert order.index("categories") < order.index("products")
    assert order.index("customers") < order.index("orders")
    assert order.index("orders") < order.index("order_items")
    assert order.index("products") < order.index("order_items")
