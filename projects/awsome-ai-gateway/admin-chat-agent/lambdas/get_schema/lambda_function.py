# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""get_schema Lambda — admin-chat-agent's schema introspection tool.

호출 입력:
  { "table_name": "auth.users" | None }   # None 이면 전체 목록 반환

호출 출력 (없으면 전체 목록):
  { "tables": [{ "schema": str, "table": str, "description": str }, ...] }

호출 출력 (table_name 지정 시):
  { "schema": str, "table": str, "description": str,
    "columns": [{ "name": str, "type": str, "description": str,
                  "sample_values": [...] }, ...] }
"""

from __future__ import annotations

import logging
import os
from typing import Any

import yaml

logger = logging.getLogger()
logger.setLevel(logging.INFO)

WHITELIST_PATH = os.environ.get(
    "SCHEMA_WHITELIST_PATH",
    "/var/task/schema_whitelist.yaml",
)

_whitelist_cache: dict[str, Any] | None = None


def get_whitelist() -> dict[str, Any]:
    global _whitelist_cache
    if _whitelist_cache is None:
        with open(WHITELIST_PATH, encoding="utf-8") as f:
            _whitelist_cache = yaml.safe_load(f)
    return _whitelist_cache


def lambda_handler(event: dict, context: Any) -> dict:
    table_name = (event or {}).get("table_name")
    wl = get_whitelist()
    tables = wl.get("allowed_tables", [])

    if not table_name:
        return {
            "tables": [
                {
                    "schema": t["schema"],
                    "table": t["table"],
                    "description": t.get("description", ""),
                }
                for t in tables
            ],
            "forbidden_columns": wl.get("forbidden_columns", []),
            "forbidden_operations": wl.get("forbidden_operations", []),
        }

    # 'auth.users' 또는 'users' 모두 허용
    if "." in table_name:
        schema_q, table_q = table_name.lower().split(".", 1)
    else:
        schema_q, table_q = None, table_name.lower()

    for t in tables:
        if t["table"].lower() != table_q:
            continue
        if schema_q and t["schema"].lower() != schema_q:
            continue
        return {
            "schema": t["schema"],
            "table": t["table"],
            "description": t.get("description", ""),
            "columns": t.get("columns", []),
        }

    return {
        "error": f"Table '{table_name}' not in whitelist. "
        f"Use get_schema() with no args to list all allowed tables.",
    }
