"""
generate_data.py — Mock 이커머스 데이터셋 생성기.

AnyCompany(가상의 온라인 쇼핑몰)의 6개월치 주문/상품/고객 데이터를 만들어
`ecommerce.db`(SQLite)와 분석용 CSV로 저장합니다. 결정적(seed 고정)이라
누가 실행해도 같은 데이터가 나옵니다.

    python generate_data.py        # -> ecommerce.db, orders.csv, products.csv
"""
import csv
import os
import random
import sqlite3
from datetime import date, timedelta

random.seed(42)
HERE = os.path.dirname(os.path.abspath(__file__))

# ── 카테고리 / 상품 마스터 ────────────────────────────────────────────────
CATEGORIES = {
    "Electronics": [("무선 이어버드", 89000), ("USB-C 충전기", 19000), ("블루투스 스피커", 65000),
                    ("기계식 키보드", 119000), ("4K 모니터", 329000)],
    "Home": [("아로마 디퓨저", 35000), ("스탠드 조명", 48000), ("주방 저울", 22000),
             ("로봇청소기", 459000), ("에어프라이어", 129000)],
    "Fashion": [("코튼 후디", 54000), ("러닝화", 98000), ("크로스백", 76000),
                ("울 머플러", 39000), ("청바지", 69000)],
    "Beauty": [("수분 크림", 42000), ("선크림 SPF50", 28000), ("립밤 세트", 18000),
               ("헤어 세럼", 33000), ("클렌징 폼", 21000)],
}
REGIONS = ["서울", "경기", "부산", "대구", "인천", "광주", "대전"]
CHANNELS = ["web", "app", "app", "app"]  # 앱 비중 높게

# 상품 ID 부여
products = []
pid = 1000
for cat, items in CATEGORIES.items():
    for name, price in items:
        products.append({"product_id": pid, "name": name, "category": cat, "price": price})
        pid += 1

# ── 주문 생성 (6개월, 일별 변동 + 주말/월말 부스트) ─────────────────────────
START = date(2026, 1, 1)
DAYS = 180
orders = []
oid = 500000
for d in range(DAYS):
    day = START + timedelta(days=d)
    base = 18 + int(8 * random.random())
    if day.weekday() >= 5:          # 주말 부스트
        base = int(base * 1.4)
    if day.day >= 25:               # 월말 페이데이 부스트
        base = int(base * 1.25)
    for _ in range(base):
        prod = random.choices(products, weights=[5 if p["category"] == "Electronics" else 3
                                                 for p in products])[0]
        qty = random.choices([1, 1, 1, 2, 3], k=1)[0]
        # 가끔 할인
        disc = random.choice([0, 0, 0, 0.1, 0.15])
        revenue = round(prod["price"] * qty * (1 - disc))
        orders.append({
            "order_id": oid,
            "order_date": day.isoformat(),
            "product_id": prod["product_id"],
            "category": prod["category"],
            "quantity": qty,
            "unit_price": prod["price"],
            "discount": disc,
            "revenue": revenue,
            "region": random.choice(REGIONS),
            "channel": random.choice(CHANNELS),
            "customer_id": 10000 + random.randint(0, 2999),
        })
        oid += 1

# ── SQLite ────────────────────────────────────────────────────────────────
db_path = os.path.join(HERE, "ecommerce.db")
if os.path.exists(db_path):
    os.remove(db_path)
con = sqlite3.connect(db_path)
cur = con.cursor()
cur.execute("""CREATE TABLE products (product_id INTEGER PRIMARY KEY, name TEXT,
               category TEXT, price INTEGER)""")
cur.execute("""CREATE TABLE orders (order_id INTEGER PRIMARY KEY, order_date TEXT,
               product_id INTEGER, category TEXT, quantity INTEGER, unit_price INTEGER,
               discount REAL, revenue INTEGER, region TEXT, channel TEXT, customer_id INTEGER)""")
cur.executemany("INSERT INTO products VALUES (:product_id,:name,:category,:price)", products)
cur.executemany("""INSERT INTO orders VALUES (:order_id,:order_date,:product_id,:category,
                :quantity,:unit_price,:discount,:revenue,:region,:channel,:customer_id)""", orders)
con.commit()

# ── CSV (사람이 열어보기 / pandas 데모용) ──────────────────────────────────
with open(os.path.join(HERE, "products.csv"), "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["product_id", "name", "category", "price"])
    w.writeheader(); w.writerows(products)
with open(os.path.join(HERE, "orders.csv"), "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(orders[0].keys()))
    w.writeheader(); w.writerows(orders)

total_rev = sum(o["revenue"] for o in orders)
print(f"products={len(products)}  orders={len(orders)}  "
      f"기간={START}~{START+timedelta(days=DAYS-1)}  총매출={total_rev:,}원")
print(f"   -> {db_path}")
con.close()
