# Copyright 2026 © Amazon.com and Affiliates.
from __future__ import annotations

import pytest

from app.services.router_service import check_client_scope


def test_none_allows_all():
    check_client_scope(None, "claude-code")   # no raise
    check_client_scope([], "cowork")          # no raise (both)


def test_whitelist_allows_member():
    check_client_scope(["cowork"], "cowork")  # no raise


def test_whitelist_denies_nonmember():
    with pytest.raises(PermissionError):
        check_client_scope(["cowork"], "claude-code")


def test_whitelist_denies_other_and_none():
    with pytest.raises(PermissionError):
        check_client_scope(["cowork"], "other")
    with pytest.raises(PermissionError):
        check_client_scope(["claude-code"], None)
