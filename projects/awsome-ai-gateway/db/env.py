# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Alembic migrations need schema modification privileges → use master user
# DB_MASTER_URL takes precedence, fallback to DB_URL for local dev
db_master = os.getenv("DB_MASTER_URL")
db_app = os.getenv("DB_URL")
db_url = db_master or db_app or config.get_main_option("sqlalchemy.url")

# create_async_engine requires postgresql+asyncpg:// format
# DB_MASTER_URL comes from Helm chart as postgresql:// (for psql compatibility)
# Convert it to postgresql+asyncpg:// for SQLAlchemy async engine
if db_url and db_url.startswith("postgresql://") and "+asyncpg" not in db_url:
    db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    # Also convert sslmode= (psql format) to ssl= (asyncpg format)
    if "?sslmode=" in db_url:
        db_url = db_url.replace("?sslmode=", "?ssl=")

# DB_MASTER_URL contains $(DB_MASTER_PASSWORD) placeholder that K8s resolves,
# but special chars in the password can break URL parsing for asyncpg.
# If DB_MASTER_PASSWORD env is set, rebuild the URL with properly quoted password.
# Cannot use urlparse here because chars like [ ] in the password break parsing.
if db_url and os.getenv("DB_MASTER_PASSWORD"):
    import re
    from urllib.parse import quote
    safe_password = quote(os.getenv("DB_MASTER_PASSWORD"), safe="")
    # Replace password between :// user : password @ host
    db_url = re.sub(r'(://[^:]+:)[^@]+(@)', rf'\g<1>{safe_password}\2', db_url, count=1)

# DEBUG: Print which URL is being used (mask password)
print(f"[ENV.PY DEBUG] DB_MASTER_URL exists: {bool(db_master)}")
print(f"[ENV.PY DEBUG] DB_URL exists: {bool(db_app)}")
if db_url:
    # Show first 40 chars to see which connection string is used
    print(f"[ENV.PY DEBUG] Using URL starting with: {db_url[:40]}...")
else:
    print(f"[ENV.PY DEBUG] ERROR: No database URL found!")


# transaction_per_migration=True commits each migration script independently
# instead of wrapping the whole upgrade in one transaction. Required for the
# enum split (0008 ALTER TYPE ADD VALUE must COMMIT before 0009 INSERTs use the
# new value, else PostgreSQL raises "unsafe use of new value of enum type").
# Safe here because every migration is idempotent (IF NOT EXISTS / ON CONFLICT).
def run_migrations_offline() -> None:
    context.configure(
        url=db_url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(db_url)
    async with engine.connect() as conn:
        await conn.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
