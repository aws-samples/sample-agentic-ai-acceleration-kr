"""e-커머스 도메인 스키마 정의 (단일 진실 원천).

테이블/컬럼 메타데이터를 코드로 선언하고, 여기서 다음을 파생한다.
- PostgreSQL DDL (CREATE TABLE + COMMENT + 인덱스) — Aurora 적재용
- semantic layer 문서 (OpenSearch 인덱싱용 컬럼/테이블 메타데이터)

COMMENT 는 semantic layer 의 원천이므로 자연어 질의에 도움이 되도록
비즈니스 의미를 한국어로 명시한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Column:
    """테이블 컬럼 정의."""

    name: str
    type: str
    comment: str
    nullable: bool = True
    primary_key: bool = False
    references: str | None = None  # "table(column)" 형식의 FK 참조
    default: str | None = None

    def ddl(self) -> str:
        parts = [self.name, self.type]
        if self.primary_key:
            parts.append("PRIMARY KEY")
        if not self.nullable and not self.primary_key:
            parts.append("NOT NULL")
        if self.default is not None:
            parts.append(f"DEFAULT {self.default}")
        if self.references is not None:
            parts.append(f"REFERENCES {self.references}")
        return " ".join(parts)


@dataclass(frozen=True)
class Index:
    """인덱스 정의."""

    name: str
    table: str
    columns: tuple[str, ...]

    def ddl(self) -> str:
        cols = ", ".join(self.columns)
        return f"CREATE INDEX IF NOT EXISTS {self.name} ON {self.table} ({cols});"


@dataclass(frozen=True)
class Table:
    """테이블 정의 (컬럼 + 코멘트 + 인덱스)."""

    name: str
    comment: str
    columns: tuple[Column, ...]
    indexes: tuple[Index, ...] = field(default_factory=tuple)

    def create_ddl(self) -> str:
        col_lines = ",\n".join(f"    {c.ddl()}" for c in self.columns)
        return f"CREATE TABLE IF NOT EXISTS {self.name} (\n{col_lines}\n);"

    def comment_ddl(self) -> list[str]:
        stmts = [f"COMMENT ON TABLE {self.name} IS {_sql_str(self.comment)};"]
        for c in self.columns:
            stmts.append(
                f"COMMENT ON COLUMN {self.name}.{c.name} IS {_sql_str(c.comment)};"
            )
        return stmts


def _sql_str(value: str) -> str:
    """작은따옴표를 이스케이프한 SQL 문자열 리터럴."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


# --- 스키마 정의 ---------------------------------------------------------

CUSTOMERS = Table(
    name="customers",
    comment="고객 마스터. 회원 가입한 개별 구매자 정보.",
    columns=(
        Column("id", "INTEGER", "고객 고유 식별자(PK).", nullable=False, primary_key=True),
        Column("name", "VARCHAR(100)", "고객 이름.", nullable=False),
        Column("email", "VARCHAR(255)", "고객 이메일(로그인 ID, 유니크).", nullable=False),
        Column(
            "region",
            "VARCHAR(20)",
            "고객 거주 지역(예: 서울, 경기, 부산 등 한국 광역 지역).",
            nullable=False,
        ),
        Column(
            "created_at",
            "TIMESTAMP",
            "회원 가입 일시.",
            nullable=False,
        ),
        Column(
            "last_login_at",
            "TIMESTAMP",
            "마지막 로그인 일시. '최근 활성 고객' 판정의 기준 컬럼.",
        ),
    ),
    indexes=(
        Index("idx_customers_region", "customers", ("region",)),
        Index("idx_customers_last_login_at", "customers", ("last_login_at",)),
    ),
)

CATEGORIES = Table(
    name="categories",
    comment="상품 카테고리. 상품이 속하는 분류.",
    columns=(
        Column("id", "INTEGER", "카테고리 고유 식별자(PK).", nullable=False, primary_key=True),
        Column("name", "VARCHAR(50)", "카테고리 이름(예: 전자제품, 의류, 식품).", nullable=False),
        Column("description", "VARCHAR(255)", "카테고리 설명."),
    ),
)

PRODUCTS = Table(
    name="products",
    comment="상품 마스터. 판매 중인 개별 상품.",
    columns=(
        Column("id", "INTEGER", "상품 고유 식별자(PK).", nullable=False, primary_key=True),
        Column("name", "VARCHAR(200)", "상품명.", nullable=False),
        Column(
            "category_id",
            "INTEGER",
            "상품이 속한 카테고리(categories.id 참조).",
            nullable=False,
            references="categories(id)",
        ),
        Column("price", "NUMERIC(12,2)", "상품 정가(원). 판매 단가의 기준.", nullable=False),
        Column("created_at", "TIMESTAMP", "상품 등록 일시.", nullable=False),
    ),
    indexes=(Index("idx_products_category_id", "products", ("category_id",)),),
)

ORDERS = Table(
    name="orders",
    comment="주문 헤더. 고객이 발생시킨 개별 주문 건.",
    columns=(
        Column("id", "INTEGER", "주문 고유 식별자(PK).", nullable=False, primary_key=True),
        Column(
            "customer_id",
            "INTEGER",
            "주문한 고객(customers.id 참조).",
            nullable=False,
            references="customers(id)",
        ),
        Column(
            "status",
            "VARCHAR(20)",
            "주문 상태(pending/paid/shipped/delivered/cancelled).",
            nullable=False,
        ),
        Column(
            "ordered_at",
            "TIMESTAMP",
            "주문 발생 일시. 기간별 매출 집계의 기준 컬럼.",
            nullable=False,
        ),
        Column(
            "total_amount",
            "NUMERIC(14,2)",
            "주문 총액(원). order_items 의 수량*단가 합계. 매출(revenue)의 기준.",
            nullable=False,
        ),
    ),
    indexes=(
        Index("idx_orders_customer_id", "orders", ("customer_id",)),
        Index("idx_orders_ordered_at", "orders", ("ordered_at",)),
        Index("idx_orders_status", "orders", ("status",)),
    ),
)

ORDER_ITEMS = Table(
    name="order_items",
    comment="주문 상세. 한 주문에 포함된 개별 상품 라인.",
    columns=(
        Column("id", "INTEGER", "주문 상세 고유 식별자(PK).", nullable=False, primary_key=True),
        Column(
            "order_id",
            "INTEGER",
            "소속 주문(orders.id 참조).",
            nullable=False,
            references="orders(id)",
        ),
        Column(
            "product_id",
            "INTEGER",
            "주문된 상품(products.id 참조).",
            nullable=False,
            references="products(id)",
        ),
        Column("quantity", "INTEGER", "주문 수량.", nullable=False),
        Column(
            "unit_price",
            "NUMERIC(12,2)",
            "주문 시점의 상품 단가(원). 주문 당시 가격 스냅샷.",
            nullable=False,
        ),
    ),
    indexes=(
        Index("idx_order_items_order_id", "order_items", ("order_id",)),
        Index("idx_order_items_product_id", "order_items", ("product_id",)),
    ),
)

# FK 의존성을 만족하는 생성/적재 순서.
TABLES: tuple[Table, ...] = (
    CATEGORIES,
    CUSTOMERS,
    PRODUCTS,
    ORDERS,
    ORDER_ITEMS,
)


def build_schema_ddl() -> str:
    """전체 스키마 DDL(CREATE TABLE + COMMENT + 인덱스)을 하나의 문자열로 반환."""
    blocks: list[str] = []
    for table in TABLES:
        blocks.append(table.create_ddl())
        blocks.extend(table.comment_ddl())
        for idx in table.indexes:
            blocks.append(idx.ddl())
    return "\n".join(blocks) + "\n"


def iter_ddl_statements() -> list[str]:
    """개별 실행 가능한 DDL statement 목록(RDS Data API용, 세미콜론 제거)."""
    statements: list[str] = []
    for table in TABLES:
        statements.append(table.create_ddl().rstrip(";"))
        statements.extend(s.rstrip(";") for s in table.comment_ddl())
        for idx in table.indexes:
            statements.append(idx.ddl().rstrip(";"))
    return statements
