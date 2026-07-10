# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Tests for statusline.formatter — display formatting and severity."""

from __future__ import annotations

from decimal import Decimal

from statusline.formatter import (
    Severity,
    StatuslineState,
    determine_severity,
    format_status,
)
from statusline.usage_client import UsageInfo


class TestDetermineSeverity:
    def test_normal(self) -> None:
        assert determine_severity(50.0, True) == Severity.NORMAL

    def test_warning_at_80(self) -> None:
        assert determine_severity(80.0, True) == Severity.WARNING

    def test_warning_at_99(self) -> None:
        assert determine_severity(99.9, True) == Severity.WARNING

    def test_critical_at_100(self) -> None:
        assert determine_severity(100.0, True) == Severity.CRITICAL

    def test_critical_over_100(self) -> None:
        assert determine_severity(120.0, True) == Severity.CRITICAL

    def test_offline(self) -> None:
        assert determine_severity(50.0, False) == Severity.OFFLINE

    def test_zero_percent(self) -> None:
        assert determine_severity(0.0, True) == Severity.NORMAL


class TestFormatStatus:
    def test_normal(self) -> None:
        state = StatuslineState(
            current=UsageInfo(
                used=Decimal("12.50"),
                limit=Decimal("100.00"),
                percentage=12.5,
            ),
            severity=Severity.NORMAL,
            is_online=True,
        )
        assert format_status(state) == "$12.50 / $100.00 (12.5%)"

    def test_warning(self) -> None:
        state = StatuslineState(
            current=UsageInfo(
                used=Decimal("82.00"),
                limit=Decimal("100.00"),
                percentage=82.0,
            ),
            severity=Severity.WARNING,
            is_online=True,
        )
        assert format_status(state) == "$82.00 / $100.00 (82.0%) [!]"

    def test_critical(self) -> None:
        state = StatuslineState(
            current=UsageInfo(
                used=Decimal("100.00"),
                limit=Decimal("100.00"),
                percentage=100.0,
            ),
            severity=Severity.CRITICAL,
            is_online=True,
        )
        assert format_status(state) == "$100.00 / $100.00 (100.0%) [!!]"

    def test_offline_with_data(self) -> None:
        state = StatuslineState(
            current=UsageInfo(
                used=Decimal("12.50"),
                limit=Decimal("100.00"),
                percentage=12.5,
            ),
            severity=Severity.OFFLINE,
            is_online=False,
        )
        assert format_status(state) == "$12.50 / $100.00 (12.5%) [offline]"

    def test_no_data(self) -> None:
        state = StatuslineState()
        assert format_status(state) == "-- / -- (--)"

    def test_decimal_formatting(self) -> None:
        state = StatuslineState(
            current=UsageInfo(
                used=Decimal("0.10"),
                limit=Decimal("50.00"),
                percentage=0.2,
            ),
            severity=Severity.NORMAL,
            is_online=True,
        )
        assert format_status(state) == "$0.10 / $50.00 (0.2%)"
