"""결정적 e-커머스 샘플 데이터 생성기.

`random.Random(SEED)` 를 사용해 seed 고정 데이터를 생성한다.
같은 seed 는 항상 같은 데이터셋을 만든다(테스트로 검증).

자연어 질의 데모에 좋은 분포를 목표로 한다.
- 지역별 매출 편차(수도권 집중)
- 최근 24개월에 걸친 주문 시간 분포 + 약한 계절성(연말 성수기)
- 카테고리별 인기 상품 편차
- 일부 비활성 고객(오래된 last_login_at)으로 '최근 활성 고객' 질의가 의미를 갖도록
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from random import Random

SEED = 42

# 기준 현재 시각(결정성 보장을 위해 고정). 데모 데이터의 '지금'.
# CLAUDE.md 의 currentDate(2026-07-26)에 맞춘 고정 기준일.
NOW = dt.datetime(2026, 7, 26, 12, 0, 0)

# 지역 분포(수도권 집중). (지역명, 가중치)
REGIONS: tuple[tuple[str, int], ...] = (
    ("서울", 30),
    ("경기", 25),
    ("부산", 10),
    ("인천", 8),
    ("대구", 7),
    ("대전", 5),
    ("광주", 5),
    ("울산", 4),
    ("강원", 3),
    ("제주", 3),
)

ORDER_STATUSES: tuple[tuple[str, int], ...] = (
    ("delivered", 55),
    ("shipped", 15),
    ("paid", 12),
    ("pending", 8),
    ("cancelled", 10),
)

CATEGORY_DEFS: tuple[tuple[str, str], ...] = (
    ("전자제품", "스마트폰, 노트북, 가전 등 전자 기기"),
    ("의류", "남성/여성 의류 및 패션 잡화"),
    ("식품", "신선식품, 가공식품, 음료"),
    ("생활용품", "주방, 청소, 욕실 등 생활 잡화"),
    ("도서", "단행본, 잡지, 전자책"),
    ("뷰티", "화장품, 스킨케어, 향수"),
    ("스포츠", "운동 용품, 아웃도어, 헬스"),
    ("완구", "장난감, 보드게임, 취미"),
)

# 카테고리별 상품명 접두어와 가격대(원). 인기/가격 편차 부여용.
# 값 = (상품명 후보, 최소가, 최대가, 인기 가중치)
_CATEGORY_PROFILE: dict[str, tuple[list[str], int, int, int]] = {
    "전자제품": (
        ["스마트폰", "노트북", "무선이어폰", "태블릿", "모니터", "키보드"],
        30000, 2500000, 30,
    ),
    "의류": (["티셔츠", "청바지", "자켓", "원피스", "니트", "코트"], 15000, 300000, 25),
    "식품": (["원두커피", "견과류", "올리브유", "라면", "과일세트", "간편식"], 3000, 80000, 20),
    "생활용품": (["세제", "수건세트", "주방칼", "물병", "수납함", "청소기"], 5000, 200000, 15),
    "도서": (["소설", "에세이", "자기계발서", "전공서적", "만화", "잡지"], 8000, 45000, 10),
    "뷰티": (["수분크림", "립스틱", "선크림", "향수", "샴푸", "마스크팩"], 9000, 180000, 18),
    "스포츠": (["요가매트", "덤벨", "러닝화", "텐트", "자전거", "축구공"], 12000, 600000, 12),
    "완구": (["보드게임", "블록", "인형", "퍼즐", "RC카", "피규어"], 6000, 250000, 8),
}

DEFAULT_CUSTOMERS = 1000
DEFAULT_PRODUCTS = 200
DEFAULT_ORDERS = 10000

_FAMILY_NAMES = "김이박최정강조윤장임한오서신권황안송류전홍"
_GIVEN_1 = "민서예지도하주지현우준서연아윤은채"
_GIVEN_2 = "준우호진혁성원영석호연아윤빈율서"


@dataclass
class Dataset:
    """생성된 전체 데이터셋. 각 리스트는 컬럼명 → 값 dict 의 행 목록."""

    customers: list[dict]
    categories: list[dict]
    products: list[dict]
    orders: list[dict]
    order_items: list[dict]

    def row_counts(self) -> dict[str, int]:
        return {
            "categories": len(self.categories),
            "customers": len(self.customers),
            "products": len(self.products),
            "orders": len(self.orders),
            "order_items": len(self.order_items),
        }


def _weighted_choice(rng: Random, choices: tuple[tuple[str, int], ...]) -> str:
    population = [c for c, _ in choices]
    weights = [w for _, w in choices]
    return rng.choices(population, weights=weights, k=1)[0]


def _make_name(rng: Random) -> str:
    return (
        rng.choice(_FAMILY_NAMES)
        + rng.choice(_GIVEN_1)
        + rng.choice(_GIVEN_2)
    )


def _iso(value: dt.datetime) -> str:
    return value.replace(microsecond=0).isoformat(sep=" ")


def _gen_categories() -> list[dict]:
    return [
        {"id": i + 1, "name": name, "description": desc}
        for i, (name, desc) in enumerate(CATEGORY_DEFS)
    ]


def _gen_customers(rng: Random, count: int) -> list[dict]:
    customers: list[dict] = []
    for i in range(1, count + 1):
        # 가입일: 최근 24개월 내 랜덤.
        created_days_ago = rng.randint(0, 730)
        created = NOW - dt.timedelta(days=created_days_ago)
        # 마지막 로그인: 가입 이후 ~ 현재 사이. 20%는 오래된 비활성 고객.
        if rng.random() < 0.2:
            # 비활성: 90~700일 전 (단, 가입일 이후)
            max_gap = max(90, (NOW - created).days)
            login_days_ago = rng.randint(90, max(90, min(max_gap, 700)))
        else:
            # 활성: 0~120일 전 (단, 가입일 이후)
            max_gap = (NOW - created).days
            login_days_ago = rng.randint(0, min(120, max_gap)) if max_gap > 0 else 0
        last_login = NOW - dt.timedelta(
            days=login_days_ago,
            hours=rng.randint(0, 23),
            minutes=rng.randint(0, 59),
        )
        if last_login < created:
            last_login = created
        name = _make_name(rng)
        customers.append(
            {
                "id": i,
                "name": name,
                "email": f"user{i:05d}@example.com",
                "region": _weighted_choice(rng, REGIONS),
                "created_at": _iso(created),
                "last_login_at": _iso(last_login),
            }
        )
    return customers


def _gen_products(rng: Random, count: int, categories: list[dict]) -> list[dict]:
    products: list[dict] = []
    for i in range(1, count + 1):
        category = rng.choice(categories)
        names, min_p, max_p, _ = _CATEGORY_PROFILE[category["name"]]
        base = rng.choice(names)
        # 가격: 로그 스케일 근사로 저가 다수/고가 소수 분포.
        span = max_p - min_p
        price = min_p + int(span * (rng.random() ** 2))
        # 100원 단위 반올림.
        price = max(min_p, (price // 100) * 100)
        created_days_ago = rng.randint(30, 730)
        products.append(
            {
                "id": i,
                "name": f"{base} {chr(ord('A') + (i % 26))}{i:03d}",
                "category_id": category["id"],
                "price": f"{price}.00",
                "created_at": _iso(NOW - dt.timedelta(days=created_days_ago)),
            }
        )
    return products


def _seasonal_month_weight(month: int) -> float:
    """연말(11~12월) 성수기 가중. 약한 계절성."""
    if month in (11, 12):
        return 1.8
    if month in (1, 6, 7):  # 신년/여름 세일 약한 증가
        return 1.2
    return 1.0


def _gen_orders_and_items(
    rng: Random,
    order_count: int,
    customers: list[dict],
    products: list[dict],
) -> tuple[list[dict], list[dict]]:
    # 활성 고객일수록 더 자주 주문하도록 가중.
    now = NOW
    customer_weights = []
    for c in customers:
        last_login = dt.datetime.fromisoformat(c["last_login_at"])
        days_since = (now - last_login).days
        # 최근 로그인일수록 높은 가중.
        customer_weights.append(3.0 if days_since <= 120 else 1.0)

    # 상품별 인기 가중(카테고리 인기 + 상품별 랜덤 인기).
    # 프로파일 값 = (상품명 후보, 최소가, 최대가, 인기 가중치)
    product_pop: dict[str, int] = {
        cat_name: profile[3] for cat_name, profile in _CATEGORY_PROFILE.items()
    }
    product_weights = []
    for p in products:
        cat_name = _category_name_by_id(p["category_id"])
        base_w = product_pop.get(cat_name, 10)
        product_weights.append(base_w * rng.uniform(0.3, 2.0))

    # 주문 발생일 분포: 최근 24개월, 월별 계절 가중.
    day_choices = list(range(730))  # 0..729일 전
    day_weights = []
    for d in day_choices:
        day = now - dt.timedelta(days=d)
        # 최근일수록 소폭 증가(성장) + 계절 가중.
        recency = 1.0 + (730 - d) / 730 * 0.5
        day_weights.append(_seasonal_month_weight(day.month) * recency)

    orders: list[dict] = []
    order_items: list[dict] = []
    item_id = 1
    for oid in range(1, order_count + 1):
        customer = rng.choices(customers, weights=customer_weights, k=1)[0]
        days_ago = rng.choices(day_choices, weights=day_weights, k=1)[0]
        ordered = now - dt.timedelta(
            days=days_ago,
            hours=rng.randint(0, 23),
            minutes=rng.randint(0, 59),
        )
        status = _weighted_choice(rng, ORDER_STATUSES)

        n_items = rng.choices([1, 2, 3, 4, 5], weights=[40, 30, 15, 10, 5], k=1)[0]
        chosen = rng.choices(products, weights=product_weights, k=n_items)
        total = 0
        line_rows: list[dict] = []
        for prod in chosen:
            qty = rng.choices([1, 2, 3], weights=[70, 22, 8], k=1)[0]
            unit_price = int(float(prod["price"]))
            total += unit_price * qty
            line_rows.append(
                {
                    "id": item_id,
                    "order_id": oid,
                    "product_id": prod["id"],
                    "quantity": qty,
                    "unit_price": f"{unit_price}.00",
                }
            )
            item_id += 1
        order_items.extend(line_rows)
        orders.append(
            {
                "id": oid,
                "customer_id": customer["id"],
                "status": status,
                "ordered_at": _iso(ordered),
                "total_amount": f"{total}.00",
            }
        )
    return orders, order_items


_CATEGORY_ID_TO_NAME: dict[int, str] = {
    i + 1: name for i, (name, _) in enumerate(CATEGORY_DEFS)
}


def _category_name_by_id(category_id: int) -> str:
    return _CATEGORY_ID_TO_NAME[category_id]


def generate(
    *,
    seed: int = SEED,
    n_customers: int = DEFAULT_CUSTOMERS,
    n_products: int = DEFAULT_PRODUCTS,
    n_orders: int = DEFAULT_ORDERS,
) -> Dataset:
    """결정적 데이터셋 생성. 같은 인자 → 같은 결과."""
    rng = Random(seed)
    categories = _gen_categories()
    customers = _gen_customers(rng, n_customers)
    products = _gen_products(rng, n_products, categories)
    orders, order_items = _gen_orders_and_items(rng, n_orders, customers, products)
    return Dataset(
        customers=customers,
        categories=categories,
        products=products,
        orders=orders,
        order_items=order_items,
    )
