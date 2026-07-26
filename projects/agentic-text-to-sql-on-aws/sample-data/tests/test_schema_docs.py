"""스키마 문서 빌더 로직 테스트."""

from __future__ import annotations

from sample_data import generator, schema, schema_docs


def test_build_documents_count():
    docs = schema_docs.build_documents()
    n_columns = sum(len(t.columns) for t in schema.TABLES)
    # table 문서 + column 문서.
    assert len(docs) == len(schema.TABLES) + n_columns


def test_documents_deterministic():
    a = schema_docs.build_documents()
    b = schema_docs.build_documents()
    assert a == b


def test_doc_ids_unique():
    docs = schema_docs.build_documents()
    ids = [d["doc_id"] for d in docs]
    assert len(ids) == len(set(ids))


def test_column_document_has_content_and_type():
    doc = schema_docs.build_column_document(schema.ORDERS, schema.ORDERS.columns[0])
    assert doc["doc_type"] == "column"
    assert doc["table"] == "orders"
    assert doc["content"]
    assert doc["data_type"]


def test_table_document_lists_columns():
    doc = schema_docs.build_table_document(schema.CUSTOMERS)
    assert doc["doc_type"] == "table"
    assert set(doc["columns"]) == {c.name for c in schema.CUSTOMERS.columns}


def test_sample_values_included_when_dataset_given():
    ds = generator.generate(n_customers=20, n_products=10, n_orders=50)
    doc = schema_docs.build_column_document(
        schema.CUSTOMERS,
        next(c for c in schema.CUSTOMERS.columns if c.name == "region"),
        ds,
    )
    assert doc["sample_values"]
    assert "예시 값" in doc["content"]


def test_fk_reference_in_content():
    col = next(c for c in schema.ORDERS.columns if c.name == "customer_id")
    doc = schema_docs.build_column_document(schema.ORDERS, col)
    assert doc["references"] == "customers(id)"
    assert "외래키" in doc["content"]


def test_table_row_count_when_dataset_given():
    ds = generator.generate(n_customers=25, n_products=10, n_orders=50)
    doc = schema_docs.build_table_document(schema.CUSTOMERS, ds)
    assert doc["row_count"] == 25
